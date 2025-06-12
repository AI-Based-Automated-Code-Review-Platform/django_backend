from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Comment, Thread # Assuming Thread model is also in .models

@receiver(post_save, sender=Comment)
def update_thread_last_comment_info(sender, instance, created, **kwargs):
    if created:
        thread = instance.thread
        thread.last_comment_at = instance.created_at
        thread.updated_at = instance.created_at # Also update thread's general activity timestamp
        thread.save(update_fields=['last_comment_at', 'updated_at']) 