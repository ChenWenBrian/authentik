"""Outpost models"""
from dataclasses import asdict, dataclass
from datetime import datetime
from json import dumps, loads
from typing import Iterable, Optional
from uuid import uuid4

from dacite import from_dict
from django.contrib.postgres.fields import ArrayField
from django.core.cache import cache
from django.db import models
from django.db.models.base import Model
from django.utils.translation import gettext_lazy as _
from guardian.shortcuts import assign_perm

from passbook.core.models import Provider, Token, TokenIntents, User
from passbook.lib.config import CONFIG


@dataclass
class OutpostConfig:
    """Configuration an outpost uses to configure it self"""

    passbook_host: str
    passbook_host_insecure: bool = False

    log_level: str = CONFIG.y("log_level")
    error_reporting_enabled: bool = CONFIG.y_bool("error_reporting.enabled")
    error_reporting_environment: str = CONFIG.y(
        "error_reporting.environment", "customer"
    )


class OutpostModel(Model):
    """Base model for providers that need more objects than just themselves"""

    def get_required_objects(self) -> Iterable[models.Model]:
        """Return a list of all required objects"""
        return [self]

    class Meta:

        abstract = True


class OutpostType(models.TextChoices):
    """Outpost types, currently only the reverse proxy is available"""

    PROXY = "proxy"


class OutpostDeploymentType(models.TextChoices):
    """Deployment types that are managed through passbook"""

    # KUBERNETES = "kubernetes"
    CUSTOM = "custom"


def default_outpost_config():
    """Get default outpost config"""
    return asdict(OutpostConfig(passbook_host=""))


class Outpost(models.Model):
    """Outpost instance which manages a service user and token"""

    uuid = models.UUIDField(default=uuid4, editable=False, primary_key=True)
    name = models.TextField()

    type = models.TextField(choices=OutpostType.choices, default=OutpostType.PROXY)
    deployment_type = models.TextField(
        choices=OutpostDeploymentType.choices,
        default=OutpostDeploymentType.CUSTOM,
        help_text=_(
            "Select between passbook-managed deployment types or a custom deployment."
        ),
    )
    _config = models.JSONField(default=default_outpost_config)

    providers = models.ManyToManyField(Provider)

    channels = ArrayField(models.TextField(), default=list)

    @property
    def config(self) -> OutpostConfig:
        """Load config as OutpostConfig object"""
        return from_dict(OutpostConfig, loads(self._config))

    @config.setter
    def config(self, value):
        """Dump config into json"""
        self._config = dumps(asdict(value))

    @property
    def health_cache_key(self) -> str:
        """Key by which the outposts health status is saved"""
        return f"outpost_{self.uuid.hex}_health"

    @property
    def health(self) -> Optional[datetime]:
        """Get outpost's health status"""
        key = self.health_cache_key
        value = cache.get(key, None)
        if value:
            return datetime.fromtimestamp(value)
        return None

    def _create_user(self) -> User:
        """Create user and assign permissions for all required objects"""
        user: User = User.objects.create(username=f"pb-outpost-{self.uuid.hex}")
        user.set_unusable_password()
        user.save()
        for model in self.get_required_objects():
            assign_perm(
                f"{model._meta.app_label}.view_{model._meta.model_name}", user, model
            )
        return user

    @property
    def user(self) -> User:
        """Get/create user with access to all required objects"""
        user = User.objects.filter(username=f"pb-outpost-{self.uuid.hex}")
        if user.exists():
            return user.first()
        return self._create_user()

    @property
    def token(self) -> Token:
        """Get/create token for auto-generated user"""
        token = Token.filter_not_expired(user=self.user, intent=TokenIntents.INTENT_API)
        if token.exists():
            return token.first()
        return Token.objects.create(
            user=self.user,
            intent=TokenIntents.INTENT_API,
            description=f"Autogenerated by passbook for Outpost {self.name}",
            expiring=False,
        )

    def get_required_objects(self) -> Iterable[models.Model]:
        """Get an iterator of all objects the user needs read access to"""
        objects = [self]
        for provider in (
            Provider.objects.filter(outpost=self).select_related().select_subclasses()
        ):
            if isinstance(provider, OutpostModel):
                objects.extend(provider.get_required_objects())
            else:
                objects.append(provider)
        return objects

    def __str__(self) -> str:
        return f"Outpost {self.name}"
