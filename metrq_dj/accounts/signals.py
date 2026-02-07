from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import Profile
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender=User)
def handle_user_save(sender, instance, created, **kwargs):
    """Handle user creation and updates"""
    if created:
        # Creating a profile for a new user
        profile, profile_created = Profile.objects.get_or_create(user=instance)
        if profile_created:
            logger.info(f"Профиль для пользователя {instance.username} создан.")
        else:
            logger.info(f"Профиль для пользователя {instance.username} уже существует.")
    else:
        # Updating a profile for an existing user
        if hasattr(instance, 'profile'):
            instance.profile.save()
            logger.info(f"Сохранён профиль для пользователя {instance.username}")
        else:
            Profile.objects.create(user=instance)
            logger.info(f"Создан профиль для пользователя {instance.username}, так как он не существовал.")
