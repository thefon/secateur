import logging
import time
import os
from typing import Optional, Union, Tuple, List, Iterable, Any, Dict
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.fields import JSONField
from django.db import models, transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.functional import cached_property

import twitter

import social_django.models
from django.utils.html import format_html

from . import tasks
from . import utils

logger = logging.getLogger(__name__)


class TwitterApiDisabled(Exception):
    pass


class User(AbstractUser):
    is_twitter_api_enabled = models.BooleanField(default=True)
    account = models.ForeignKey(
        "Account", null=True, editable=False, on_delete=models.SET_NULL
    )

    token_bucket_rate = models.FloatField(default=1.0)
    token_bucket_max = models.FloatField(default=200_000.0)
    token_bucket_time = models.FloatField(default=time.time)
    token_bucket_value = models.FloatField(default=200_000.0)

    @property
    def token_bucket(self) -> utils.TokenBucket:
        return utils.TokenBucket(
            time=self.token_bucket_time,
            rate=self.token_bucket_rate,
            max=self.token_bucket_max,
            value=self.token_bucket_value,
        )

    @token_bucket.setter
    def token_bucket(self, value: utils.TokenBucket) -> None:
        self.token_bucket_rate = value.rate
        self.token_bucket_max = value.max
        self.token_bucket_time = value.time
        self.token_bucket_value = value.value

    @property
    def current_tokens(self) -> int:
        return int(self.token_bucket.value_at(time.time()))

    def withdraw_tokens(self, value: int) -> None:
        if value > self.current_tokens:
            raise ValueError("Rate limit exceeded.")
        self.token_bucket = self.token_bucket.withdraw(time=time.time(), value=value)

    @cached_property
    def twitter_social_auth(self) -> social_django.models.UserSocialAuth:
        """Get the social_auth object for this user."""
        return social_django.models.UserSocialAuth.objects.get(
            user=self, provider="twitter"
        )

    @cached_property
    def twitter_user_id(self) -> int:
        return int(self.twitter_social_auth.uid)

    @cached_property
    def api(self) -> twitter.Api:
        if not self.is_twitter_api_enabled:
            raise TwitterApiDisabled()
        access_token = self.twitter_social_auth.extra_data.get("access_token")
        api = twitter.Api(
            consumer_key=os.environ.get("CONSUMER_KEY"),
            consumer_secret=os.environ.get("CONSUMER_SECRET"),
            access_token_key=access_token.get("oauth_token"),
            access_token_secret=access_token.get("oauth_token_secret"),
            sleep_on_rate_limit=False,
        )
        return api

    def get_account_by_screen_name(self, screen_name: str) -> "Account":
        queryset = Account.objects.filter(screen_name_lower=screen_name.lower())
        if queryset:
            return queryset.get()
        else:
            logger.debug("Fetching user %s from Twitter API.", screen_name)
            return tasks.get_user(self.pk, screen_name=screen_name)

    @classmethod
    def remove_unneeded_credentials(cls) -> None:
        """Remove the oauth credentials we don't need.

        We only need to keep the oauth credentials for users who are (a) logged
        in, or (b) have pending scheduled operations (like unblocks or unmutes).

        We can determine who's logged in by enforcing a max cookie age and
        looking at User.last_login, and we can see scheduled unblocks in the
        database. So we can remove the oauth creds from everyone else, reducing
        our exposure... Anyone who logged in to secateur but hasn't used it
        won't have credentials floating around in it.
        """
        delta = timedelta(days=1)
        threshold = timezone.now() - delta
        # exclude the ones that have already had their credentials removed.
        objects = social_django.models.UserSocialAuth.objects.exclude(extra_data=None)
        # include only "twitter" ones (shoudl be all of them)
        objects = objects.filter(provider="twitter")
        # Exclude any with pending unblock operations
        objects = objects.exclude(
            user__account__relationship_subject_set__until__isnull=False
        )
        # exclude anyone who's logged in recently
        objects = objects.exclude(user__last_login__gt=threshold)
        # Finally, remove the extra_data from them.
        logger.info("Removing oauth_credentials for: %r", objects)
        objects.update(extra_data=None)


def json_getter(property_name: str) -> property:
    """Returns a class property that dereferences the json dictionary."""

    def f(self: Profile) -> Union[int, str]:
        return self.json.get(property_name)

    f.__name__ = property_name
    return property(f)


class Profile(models.Model):
    user_id = models.BigIntegerField(primary_key=True, editable=False)
    json = JSONField(editable=False)

    @classmethod
    def update(
        cls, twitter_user: twitter.User, now: datetime
    ) -> "Tuple[Profile, Account]":
        """Create or update Profile/Account objects from a twitter.User instance.

        Returns a tuple of (profile, account) model instances."""
        id = twitter_user.id
        profile, profile_updated = cls.objects.update_or_create(
            user_id=id, defaults={"json": twitter_user.AsDict()}
        )
        created_at: Optional[datetime]
        try:
            if twitter_user.created_at:
                created_at = parsedate_to_datetime(twitter_user.created_at)
            else:
                created_at = None
        except Exception as e:
            created_at = None

        account, account_updated = Account.objects.update_or_create(
            user_id=id,
            defaults={
                "screen_name": twitter_user.screen_name,
                "screen_name_lower": twitter_user.screen_name.lower(),
                "name": twitter_user.name,
                "profile_updated": now,
                "profile": profile,
                "description": twitter_user.description,
                "location": twitter_user.location,
                "profile_image_url_https": twitter_user.profile_image_url_https,
                "profile_banner_url": twitter_user.profile_banner_url,
                "favourites_count": twitter_user.favourites_count,
                "followers_count": twitter_user.followers_count,
                "friends_count": twitter_user.friends_count,
                "statuses_count": twitter_user.statuses_count,
                "listed_count": twitter_user.listed_count,
                "created_at": created_at,
            },
        )
        return profile, account


for attribute_name in [
    "description",
    "screen_name",
    "location",
    "name",
    "followers_count",
    "friends_count",
    "statuses_count",
    "favourites_count",
]:
    setattr(Profile, attribute_name, json_getter(attribute_name))


class Account(models.Model):
    """A Twitter account"""

    class Meta:
        indexes = (models.Index(fields=["screen_name"]),)

    user_id = models.BigIntegerField(primary_key=True, editable=False)
    screen_name_lower = models.CharField(max_length=30, null=True, editable=False)

    profile = models.OneToOneField(
        Profile, on_delete=models.SET_NULL, null=True, editable=False
    )
    profile_updated = models.DateTimeField(null=True, editable=False)

    # TWITTER PROFILE FIELDS
    screen_name = models.CharField(max_length=30, null=True, editable=False)
    name = models.CharField(max_length=200, null=True, editable=False)
    description = models.CharField(max_length=200, null=True, editable=False)
    location = models.CharField(max_length=200, null=True, editable=False)
    profile_image_url_https = models.CharField(
        max_length=200, null=True, editable=False
    )
    profile_banner_url = models.CharField(max_length=200, null=True, editable=False)
    created_at = models.DateTimeField(null=True, editable=False)
    favourites_count = models.IntegerField(null=True, editable=False)
    followers_count = models.IntegerField(null=True, editable=False)
    friends_count = models.IntegerField(null=True, editable=False)
    statuses_count = models.IntegerField(null=True, editable=False)
    listed_count = models.IntegerField(null=True, editable=False)

    def __str__(self) -> str:
        return "{}".format(
            self.screen_name if self.screen_name is not None else f"id={self.user_id}"
        )

    @classmethod
    def get_account(
        cls, arg: Union[int, twitter.User], now: datetime = None
    ) -> "Account":
        return cls.get_accounts(arg, now=now).get()

    @classmethod
    @transaction.atomic
    def get_accounts(
        cls, *args: Union[int, twitter.User], now: Optional[datetime] = None
    ) -> "QuerySet[Account]":
        """Update account objects from a result returned from the Twitter API.

        Twitter API calls either return lists of big-integer User IDs, or lists
        of instances of 'twitter.model.User' objects.

        Either way, we need to create an 'Account' object for each twitter ID
        we see, and if we see a User object we also want to update the profile
        and 'screen_name' of the Account.

        This method unmagically does the right thing with whatever you pass it.
        """
        if not args:
            # If we didn't get anything, return an empty queryset.
            return cls.objects.none()
        if isinstance(args[0], int):
            if len(args) == 1:
                # The simplest case, make one and return it.
                account, account_created = cls.objects.get_or_create(user_id=args[0])
                return cls.objects.filter(user_id=args[0])
            else:
                # Create a bunch of account objects as efficiently as possible.
                # This tries to do clever bulk_create stuff coz it'll usually
                # be a list of 5000 numbers passed in.

                # Nab the IDs of all the already-existing Account objects.
                existing = set(
                    cls.objects.filter(user_id__in=args).values_list(
                        "user_id", flat=True
                    )
                )
                # Work out which objects we need to create
                to_create = set(args) - existing
                # Create the missing account objects a single SQL query and bulk_create
                cls.objects.bulk_create(
                    (cls(user_id=user_id) for user_id in to_create),
                    ignore_conflicts=True,
                )
                # finally, return an un-materialized queryset of all the new objects.
                return cls.objects.filter(user_id__in=args)
        elif isinstance(args[0], twitter.User):
            # If we're dealing with dicts, we need need to do it the boring
            # way with a couple SQL queries per object. I'd sure love to make this
            # cleverer.
            if now is None:
                now = timezone.now()
            ids = []
            for twitter_user in args:
                profile, account = Profile.update(twitter_user, now)
                ids.append(account.user_id)
            return cls.objects.filter(user_id__in=ids)
        raise Exception("Couldn't handle arguments %r" % (args,))

    @property
    def twitter_url(self) -> str:
        """URL for this account on twitter.com"""
        return f"https://twitter.com/i/user/{self.user_id}/"

    @property
    def blocks(self) -> "QuerySet[Account]":
        return Account.objects.filter(
            relationship_object_set__type=Relationship.BLOCKS,
            relationship_object_set__subject_id=self,
        )

    @property
    def friends(self) -> "QuerySet[Account]":
        return Account.objects.filter(
            relationship_object_set__type=Relationship.FOLLOWS,
            relationship_object_set__subject_id=self,
        )

    @property
    def followers(self) -> "QuerySet[Account]":
        return Account.objects.filter(
            relationship_subject_set__type=Relationship.FOLLOWS,
            relationship_subject_set__object_id=self,
        )

    @property
    def mutes(self) -> "QuerySet[Account]":
        return Account.objects.filter(
            relationship_object_set__type=Relationship.MUTES,
            relationship_object_set__subject_id=self,
        )

    def follows(
        self, user_id: Optional[int] = None, screen_name: Optional[str] = None
    ) -> bool:
        """Return True if self follows the user specified in either user_id or screen_name"""
        assert (
            user_id is not None or screen_name is not None
        ), "Must specify either user_id or screen_name"
        assert (
            user_id is None or screen_name is None
        ), "Must not specify both user_id and screen_name"
        if user_id is not None:
            return self.friends.filter(user_id=user_id).exists()
        else:
            return self.friends.filter(screen_name=screen_name).exists()

    def add_blocks(
        self,
        new_blocks: "Iterable[Account]",
        updated: datetime,
        until: Optional[datetime] = None,
    ) -> "QuerySet[Relationship]":
        return Relationship.add_relationships(
            subjects=[self],
            type=Relationship.BLOCKS,
            objects=new_blocks,
            updated=updated,
            until=until,
        )

    def remove_blocks_older_than(self, updated: datetime) -> int:
        return Relationship.remove_relationships(
            subject=self, type=Relationship.BLOCKS, updated__lt=updated
        )

    def add_followers(
        self, new_followers: "Iterable[Account]", updated: datetime
    ) -> "QuerySet[Relationship]":
        return Relationship.add_relationships(
            subjects=new_followers,
            type=Relationship.FOLLOWS,
            objects=[self],
            updated=updated,
        )

    def remove_followers_older_than(self, updated: datetime) -> int:
        return Relationship.remove_relationships(
            type=Relationship.FOLLOWS, object=self, updated__lt=updated
        )

    def add_friends(
        self, new_friends: "Iterable[Account]", updated: datetime
    ) -> "QuerySet[Relationship]":
        return Relationship.add_relationships(
            subjects=[self],
            type=Relationship.FOLLOWS,
            objects=new_friends,
            updated=updated,
        )

    def remove_friends_older_than(self, updated: datetime) -> int:
        return Relationship.remove_relationships(
            type=Relationship.FOLLOWS, subject=self, updated__lt=updated
        )

    def add_mutes(
        self, new_mutes: "Iterable[Account]", updated: datetime
    ) -> "QuerySet[Relationship]":
        return Relationship.add_relationships(
            subjects=[self], type=Relationship.MUTES, objects=new_mutes, updated=updated
        )

    def remove_mutes_older_than(self, updated: datetime) -> int:
        return Relationship.remove_relationships(
            subject=self, type=Relationship.MUTES, updated__lt=updated
        )


class Relationship(models.Model):
    class Meta:
        unique_together = (("type", "subject", "object"),)
        indexes = (
            models.Index(fields=["type", "subject"]),
            models.Index(fields=["type", "object"]),
        )

    FOLLOWS = 1
    BLOCKS = 2
    MUTES = 3

    TYPE_CHOICES = ((FOLLOWS, "follows"), (BLOCKS, "blocks"), (MUTES, "mutes"))

    subject = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        editable=False,
        related_name="relationship_subject_set",
    )
    type = models.IntegerField(choices=TYPE_CHOICES, editable=False)
    object = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        editable=False,
        related_name="relationship_object_set",
    )
    updated = models.DateTimeField(editable=False)
    until = models.DateTimeField(blank=True, null=True)

    def __str__(self) -> str:
        return "{subject} {type} {object}".format(
            subject=self.subject, type=self.get_type_display(), object=self.object
        )

    @classmethod
    @transaction.atomic
    def add_relationships(
        cls,
        type: int,
        subjects: Iterable[Account],
        objects: Iterable[Account],
        updated: datetime,
        until: Optional[datetime] = None,
    ) -> "QuerySet[Relationship]":
        existing = cls.objects.filter(
            type=type, subject__in=subjects, object__in=objects
        )
        existing_set = set(existing.values_list("subject", "object"))
        to_create = []
        for object in objects:
            for subject in subjects:
                if (subject.pk, object.pk) not in existing_set:
                    to_create.append(
                        cls(
                            type=type,
                            subject=subject,
                            object=object,
                            updated=updated,
                            until=until,
                        )
                    )
        cls.objects.bulk_create(to_create)
        if until:
            existing.update(updated=updated, until=until)
        else:
            existing.update(updated=updated)
        return cls.objects.filter(type=type, subject__in=subjects, object__in=objects)

    @classmethod
    def remove_relationships(cls, **kwargs: Any) -> int:
        relationships = cls.objects.filter(**kwargs)
        if relationships:
            logger.debug("Removing relationships: {}".format(relationships))
        return relationships.delete()[0]


class LogMessage(models.Model):
    class Meta:
        indexes = (models.Index(fields=["user", "-time"]),)

    class Action(models.IntegerChoices):
        GET_USER = 1
        CREATE_BLOCK = 2
        DESTROY_BLOCK = 3
        CREATE_MUTE = 4
        DESTROY_MUTE = 5
        GET_FOLLOWERS = 6
        GET_FRIENDS = 7
        GET_BLOCKS = 8
        GET_MUTES = 9
        MUTE_FOLLOWERS = 10
        BLOCK_FOLLOWERS = 11
        LOG_IN = 12
        LOG_OUT = 13
        DISCONNECT = 14

    user = models.ForeignKey(User, null=True, on_delete=models.CASCADE)
    time = models.DateTimeField()
    message = models.CharField(max_length=100, null=True)
    action = models.IntegerField(choices=Action.choices, null=True)
    account = models.ForeignKey(Account, null=True, on_delete=models.SET_NULL)
    until = models.DateTimeField(null=True)

    def format_message(self) -> str:
        if self.action == self.Action.CREATE_BLOCK:
            assert self.account is not None
            return format_html(
                'blocked {} (<a href="{}">@{}</a>){}',
                self.account.name,
                self.account.twitter_url,
                self.account.screen_name,
                f' until {self.until.strftime("%B %d, %Y")}' if self.until else "",
            )
        elif self.action == self.Action.CREATE_MUTE:
            assert self.account is not None
            return format_html(
                'muted {} (<a href="{}">@{}</a>){}',
                self.account.name,
                self.account.twitter_url,
                self.account.screen_name,
                f' until {self.until.strftime("%B %d, %Y")}' if self.until else "",
            )
        elif self.action == self.Action.BLOCK_FOLLOWERS:
            assert self.account is not None
            return format_html(
                'started blocking followers of {} (<a href="{}">@{}</a>)',
                self.account.name,
                self.account.twitter_url,
                self.account.screen_name,
            )
        elif self.action == self.Action.MUTE_FOLLOWERS:
            assert self.account is not None
            return format_html(
                'started muting followers of {} (<a href="{}">@{}</a>)',
                self.account.name,
                self.account.twitter_url,
                self.account.screen_name,
            )
        else:
            return format_html("{}", self.message)
