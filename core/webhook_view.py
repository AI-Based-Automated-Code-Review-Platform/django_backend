from django.http import HttpResponse

from .tasks.review_tasks import process_webhook_event
from .models import (
    Repository as DBRepository,
    WebhookEventLog
)
import hashlib
import hmac
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
import logging
from django.views.decorators.http import require_POST
import json
# Create a logger instance
logger = logging.getLogger(__name__)

@csrf_exempt
@require_POST
async def github_webhook(request):
    """Handle GitHub webhook events"""
    signature = request.headers.get('X-Hub-Signature-256')
    event_type = request.headers.get('X-GitHub-Event')
    delivery_id = request.headers.get('X-GitHub-Delivery')

    if not all([signature, event_type, delivery_id]):
        logger.warning("Webhook request missing required headers (Signature, Event, Delivery ID).")
        return HttpResponse('Missing required headers', status=400)

    # Find repository for this webhook
    repository = None
    try:
        payload = json.loads(request.body.decode('utf-8'))
        repo_full_name = payload.get('repository', {}).get('full_name')
        if repo_full_name:
            try:
                repository = await DBRepository.objects.aget(repo_name=repo_full_name)
            except DBRepository.DoesNotExist:
                logger.warning(f"Repository {repo_full_name} not found in the database.")
    except json.JSONDecodeError:
        logger.warning("Could not parse request body as JSON to identify repository.")

    log_entry, created = await WebhookEventLog.objects.aupdate_or_create(
        event_id=delivery_id,
        defaults={
            'repository':repository,
            'event_type': event_type,
            'payload': {'message': 'Event received, pending verification.'}, # Store raw body initially if possible
            'headers': dict(request.headers),
            'status': 'received'
        }
    )
    if not created:
        log_entry.status = 'received'
        log_entry.repository = repository
        log_entry.error_message = None
        log_entry.processed_at = None
        log_entry.payload = {'message': 'Event re-received, pending verification.'}
        log_entry.headers = dict(request.headers)
        await log_entry.asave()

    # Extract repo info from payload to find the correct secret
    try:
        payload = json.loads(request.body.decode('utf-8'))
        repo_full_name = payload.get('repository', {}).get('full_name')
        
        # Try to find repository-specific secret
        webhook_secret = None
        if repo_full_name:
            try:
                repo = await DBRepository.objects.aget(repo_name=repo_full_name)
                webhook_secret = repo.webhook_secret
            except DBRepository.DoesNotExist:
                pass
        
        # Fall back to global secret if no repo-specific secret found
        if not webhook_secret:
            webhook_secret = settings.GITHUB_WEBHOOK_SECRET
            
        # Verify signature with the appropriate secret
        expected_signature = hmac.new(
            webhook_secret.encode('utf-8'),
            request.body,
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(f"sha256={expected_signature}", signature):
            logger.warning(f"Invalid webhook signature for event {delivery_id}.")
            log_entry.status = 'failed'
            log_entry.error_message = "Invalid signature."
            await log_entry.asave()
            return HttpResponse('Invalid signature', status=401)
    except Exception as e:
        logger.error(f"Error during webhook signature verification for event {delivery_id}: {str(e)}")
        log_entry.status = 'failed'
        log_entry.error_message = f"Signature verification error: {str(e)}"
        await log_entry.asave()
        return HttpResponse('Error verifying signature', status=500)

    try:
        event_data = json.loads(request.body.decode('utf-8'))
        log_entry.payload = event_data # Update with parsed JSON payload
        await log_entry.asave(update_fields=['payload'])

        # Dispatch to Celery task for actual processing
        process_webhook_event.delay(event_type, event_data)
        
        log_entry.status = 'processed' # Mark as successfully enqueued
        log_entry.processed_at = timezone.now()
        await log_entry.asave(update_fields=['status', 'processed_at'])
        logger.info(f"Webhook event {delivery_id} ({event_type}) successfully enqueued for processing.")
        return HttpResponse('Webhook processed and enqueued', status=202)
                
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON payload for webhook event {delivery_id}: {str(e)}")
        log_entry.status = 'failed'
        log_entry.error_message = f"Invalid JSON payload: {str(e)}"
        await log_entry.asave()
        return HttpResponse('Invalid JSON payload', status=400)
    except Exception as e:
        logger.error(f"Error processing webhook event {delivery_id} after verification: {str(e)}", exc_info=True)
        log_entry.status = 'failed'
        log_entry.error_message = f"Internal processing error: {str(e)}"
        await log_entry.asave()
        return HttpResponse('Error processing webhook', status=500)