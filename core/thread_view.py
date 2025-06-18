from rest_framework.response import Response
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from asgiref.sync import async_to_sync

from .tasks.review_tasks import calculate_cost
from .models import (
    User,
    Thread as ThreadModel,
    Comment as CommentModel,
    LLMUsage as LLMUsageModel,
)
from .serializers import (
    ReviewSerializer, ThreadSerializer, CommentSerializer
)
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
import logging
from core.langgraph_client.client import LangGraphClient
import asyncio
from .permissions import (CanAccessRepository, IsAssignedReviewerForThread)
# Create a logger instance
logger = logging.getLogger(__name__)

class ThreadViewSet(viewsets.ModelViewSet):
    serializer_class = ThreadSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ThreadModel.objects.filter(
            Q(review__repository__owner=self.request.user) |
            Q(review__repository__collaborators__user=self.request.user)
        ).distinct()
    # the permission for reply should include isAssignedReviewerForThread only remove it for testing
    # @action(detail=True, methods=['post'], url_path='reply', permission_classes=[IsAuthenticated, isAssignedReviewerForThread])
    @action(detail=True, methods=['post'], url_path='reply', permission_classes=[IsAuthenticated])
    def reply(self, request, pk=None):
        """
        Reply to a thread and get an AI response.
        
        Args:
            pk: The ID of the Thread model instance
            
        Request body:
            message: str - The user's reply message
            
        Returns:
            Response with user comment and AI response
        """
        thread = self.get_object()
        # Validate the input
        message = request.data.get('message')
        parent_comment_id = request.data.get('parent_comment_id')  # Add this
         # Get parent comment if specified
        parent_comment = None
        if parent_comment_id:
            try:
                parent_comment = CommentModel.objects.get(id=parent_comment_id, thread=thread)
            except CommentModel.DoesNotExist:
                return Response(
                    {"detail": "Parent comment not found"},
                    status=status.HTTP_404_NOT_FOUND
                )
        if not message:
            return Response(
                {"detail": "Message is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create user's comment
        user_comment = CommentModel.objects.create(
            thread=thread,
            user=request.user,
            comment=message,
            type='request',  # This is a request for AI feedback
            parent_comment=parent_comment
        )
        
        # Get AI user (create if not exists)
        ai_user, _ = User.objects.get_or_create(
            username="ai_assistant",
            defaults={
                "email": "ai@example.com",
                "is_staff": True,
                "is_ai_user": True  # Assuming this field exists
            }
        )
        
        # If no AI user ID is configured, use the first admin user
        if not ai_user and not settings.AI_USER_ID:
            ai_user = User.objects.filter(is_staff=True).first()
            if not ai_user:
                logger.error("No AI user or admin user found for AI responses")
                return Response(
                    {"detail": "Server configuration error: No AI user available"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
        elif settings.AI_USER_ID and not ai_user:
            try:
                ai_user = User.objects.get(id=settings.AI_USER_ID)
            except User.DoesNotExist:
                logger.error(f"AI_USER_ID {settings.AI_USER_ID} not found")
                # Continue with admin user
        # Manage asyncio event loop explicitly using async_to_sync helpers
        # Process user's message with LangGraph
        try:
            # Get thread history to provide context for the conversation
            thread_comments = CommentModel.objects.filter(thread=thread).order_by('created_at')
            conversation_history = []
            
            # for comment in thread_comments:
            #     # Skip the most recent comment (the one we just added)
            #     if comment.id == user_comment.id:
            #         continue
                    
            #     role = "ai" if comment.user.is_staff or getattr(comment.user, 'is_ai_user', False) else "user"
            #     conversation_history.append((role, comment.message))
            
            # Add the current user message
            conversation_history.append(("user", message))
            
            # Get LangGraph service
            langgraph_client_instance = LangGraphClient()
            async_to_sync(langgraph_client_instance.initialize)()
            # Determine if this is the first message in the thread
            is_first_message_in_thread = thread_comments.count() <= 1 # Only our new comment
            print(f"Is first message in thread: {is_first_message_in_thread} , {thread_comments.count()} comments in thread")
            # Fetch review data and repo settings for context if it's the first message
            review_model_instance = thread.review
            review_data_for_lg = {}
            repo_settings_for_lg = {}

            if is_first_message_in_thread:
                # Serialize review and repository for context
                # This is a simplified example; you might need more detailed serialization
                review_data_for_lg = ReviewSerializer(review_model_instance).data
                
                repo_settings_for_lg = {
                    'llm_preference': review_model_instance.repository.llm_preference or settings.DEFAULT_LLM_MODEL,
                    'coding_standards': review_model_instance.repository.coding_standards or [],
                    'code_metrics': review_model_instance.repository.code_metrics or [],
                } 
            response = async_to_sync(langgraph_client_instance.handle_feedback)(
                feedback=message,
                thread_id=thread.thread_id,  # This is the LangGraph native thread_id
                user_id=str(request.user.github_id),
                # conversation_history=conversation_history, # If you want to pass the history
                is_first_message=is_first_message_in_thread,
                review_data=review_data_for_lg,
                repo_settings=repo_settings_for_lg
            )
            # response = async_to_sync(langgraph_client_instance.handle_feedback)(
            #     # review_id=str(thread.review.id),
            #     feedback=message,
            #     thread_id=thread.thread_id, # This is the LangGraph native thread_id
            #     user_id=str(request.user.id),
            #     # conversation_history=conversation_history,
            #     is_first_message=is_first_message_in_thread,
            #     review_data=review_data_for_lg,
            #     repo_settings=repo_settings_for_lg
            # )
            # Extract AI response and token usage
            ai_response_content = response.get('feedback_data', {}).get('messages', [])[-1]
            # Extract the last AI message, assuming it's the latest response
            actual_ai_message = "No response generated."
            if ai_response_content and isinstance(ai_response_content, tuple) and ai_response_content[0] == 'ai':
                actual_ai_message = ai_response_content[1]
            elif isinstance(ai_response_content, dict) and ai_response_content.get('type') == 'ai': # Adjust if structure is different
                 actual_ai_message = ai_response_content.get('content', actual_ai_message)


            token_usage = response.get('token_usage', {})
            
            # Filter the feedback_data before saving
            raw_feedback_data = response.get('feedback_data', {})
            allowed_keys = [
                "repo", "user", "fixes", "pr_id", "llm_model", "metrics", 
                "reviews", "original_review", "updated_review", "messages", 
                "feedback", "standards", "re_run_plan", "reviewer_id", 
                "sufficiency", "instructions", "feedback_status", 
                "feedback_suggestion", "feedback_explanation", 
                "sufficiency_suggestion", "sufficiency_explanation"
            ]
            filtered_feedback_data = {key: raw_feedback_data[key] for key in allowed_keys if key in raw_feedback_data}
            
            ai_comment = CommentModel.objects.create(
                thread=thread,
                user=ai_user,
                comment=actual_ai_message, # Use extracted message
                type='response',
                parent_comment=user_comment,
                comment_data=filtered_feedback_data # Store the relevant part of the response
            )
            
            # Update thread with last_comment reference and timestamp
            # thread.last_comment = ai_comment
            thread.last_comment_at = timezone.now()
            thread.save(update_fields=['last_comment_at'])
            
            # Record token usage
            if token_usage:
                LLMUsageModel.objects.create(
                    review=thread.review,
                    user=request.user,
                    llm_model=thread.review.repository.llm_preference or settings.DEFAULT_LLM_MODEL,
                    input_tokens=token_usage.get('input_tokens', 0),
                    output_tokens=token_usage.get('output_tokens', 0),
                    cost=calculate_cost(token_usage, thread.review.repository.llm_preference or settings.DEFAULT_LLM_MODEL),
                )
            
            # Return both user comment and AI response
            return Response({
                'user_comment': CommentSerializer(user_comment).data,
                'ai_response': CommentSerializer(ai_comment).data,
                'token_usage': token_usage
            })
            
        except Exception as e:
            logger.error(f"Error processing thread reply: {str(e)}", exc_info=True)
            
            # If we fail, we should still show the user's comment but explain the error
            error_message = f"I'm sorry, I couldn't process your request: {str(e)}"
            
            # Try to create an error response from the AI
            try:
                ai_comment = CommentModel.objects.create(
                    thread=thread,
                    user=ai_user,
                    parent_comment=user_comment,
                    comment=error_message,
                    type='error',
                    comment_data={'error': str(e)}
                )
                
                # thread.last_comment = ai_comment
                thread.last_comment_at = timezone.now()
                thread.save(update_fields=['last_comment_at'])
                
                return Response({
                    'user_comment': CommentSerializer(user_comment).data,
                    'ai_response': CommentSerializer(ai_comment).data,
                    'error': str(e)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
            except Exception as inner_e:
                logger.error(f"Failed to create error comment: {str(inner_e)}")
                # Return just the user comment if everything else fails
                return Response({
                    'user_comment': CommentSerializer(user_comment).data,
                    'error': str(e)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

