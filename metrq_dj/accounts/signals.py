# This file is imported via accounts/apps.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import Profile
import logging


logger = logging.getLogger(__name__)


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """Auto-create profile when user is created"""
    if created:
        profile, profile_created = Profile.objects.get_or_create(user=instance)
        if profile_created:
            logger.info(f"Профиль для пользователя {instance.username} создан.")
        else:
            logger.info(f"Профиль для пользователя {instance.username} уже существует.")


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    """Save profile when user is saved"""
    if hasattr(instance, 'profile'):
        instance.profile.save()
        logger.info(f"Сохранён профиль для пользователя {instance.username}")
    else:
        Profile.objects.create(user=instance)
        logger.info(f"Создан профиль для пользователя {instance.username}, так как он не существовал.")
        