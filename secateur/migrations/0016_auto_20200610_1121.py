# Generated by Django 3.0.7 on 2020-06-09 23:21

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("secateur", "0015_auto_20200610_1043"),
    ]

    operations = [
        migrations.AlterField(
            model_name="account",
            name="location",
            field=models.CharField(editable=False, max_length=200, null=True),
        ),
        migrations.AlterField(
            model_name="account",
            name="name",
            field=models.CharField(editable=False, max_length=200, null=True),
        ),
    ]
