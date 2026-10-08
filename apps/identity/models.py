from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.db.models.functions import Lower


class UserManager(DjangoUserManager):
    def _create_user(self, username, email, password, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        return super()._create_user(username, email.strip().lower(), password, **extra_fields)

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_platform_admin", True)
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    """Global identity only. Business roles live on TenantMembership."""

    email = models.EmailField(unique=True)
    is_platform_admin = models.BooleanField(default=False)
    objects = UserManager()

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="identity_email_case_unique")]

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        return super().save(*args, **kwargs)

    @property
    def platform_operator(self):
        return self.is_active and self.is_superuser and self.is_platform_admin
