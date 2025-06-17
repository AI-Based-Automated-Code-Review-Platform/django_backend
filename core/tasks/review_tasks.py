import json
import logging
from typing import Dict, Any
import uuid
from celery import shared_task
from django.conf import settings
import asyncio
from ..models import Review, Repository, PullRequest, LLMUsage, User, Commit, Thread
from core.langgraph_client.client import LangGraphClient
from core.services import GitHubService
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.utils import timezone

logger = logging.getLogger(__name__)

@shared_task(bind=True)
def process_webhook_event(self, event_type: str, event_data: Dict[str, Any]) -> None:
    """Process webhook events asynchronously by dispatching to specific task handlers."""
    logger.info(f"Received webhook event: {event_type} with action {event_data.get('action')}")
    try:
        if event_type == 'pull_request':
            repo_data = event_data.get('repository', {})
            pr_data = event_data.get('pull_request', {})
            action = event_data.get('action')

            if not repo_data or not pr_data or not action:
                logger.error("Missing repository, pull_request, or action data in PR webhook event.")
                return

            repo_full_name = repo_data.get('full_name')
            pr_number = pr_data.get('number')

            if not repo_full_name or not pr_number:
                logger.error(f"Missing repo_full_name or pr_number for PR event. Data: {event_data}")
                return
            
            try:
                repo = Repository.objects.get(repo_name=repo_full_name)
                pr, pr_created = PullRequest.objects.update_or_create(
                    repository=repo,
                    pr_number=pr_number,
                    defaults={
                        'url': pr_data.get('html_url'),
                        'title': pr_data.get('title'),
                        # 'pr_author': pr_data.get('user', {}).get('login'),
                        'author_github_id': pr_data.get('user', {}).get('id'),
                        'body': pr_data.get('body'),
                        'status': pr_data.get('state'),
                        'pr_github_id': str(pr_data.get('id')),
                        'head_sha': pr_data.get('head', {}).get('sha'),
                        'base_sha': pr_data.get('base', {}).get('sha'),
                        # 'updated_at_gh': pr_data.get('updated_at'),
                    }
                )
                if pr_created:
                    logger.info(f"PR #{pr_number} for repo {repo_full_name} CREATED in DB via webhook task.")
                else:
                    logger.info(f"PR #{pr_number} for repo {repo_full_name} UPDATED in DB via webhook task.")

                if action in ['opened', 'reopened', 'synchronize']:
                    review, review_created = Review.objects.get_or_create(
                        repository=repo,
                        pull_request=pr,
                        status__in=['pending', 'in_progress'],
                        defaults={
                            'status': 'pending',
                            'review_data': {'message': f'Review initiated by webhook action: {action}.'}
                        }
                    )
                    if review_created:
                        logger.info(f"PENDING review record CREATED for PR {pr.id}. Enqueuing process_pr_review.")
                        process_pr_review.delay(event_data, repo.id, pr.id, triggering_user_id=repo.owner.id)
                    elif review.status == 'pending':
                        logger.info(f"PENDING review record already exists for PR {pr.id}. Enqueuing process_pr_review.")
                        process_pr_review.delay(event_data, repo.id, pr.id, triggering_user_id=repo.owner.id)
                    else:
                        logger.info(f"Review for PR {pr.id} already in progress or completed. Status: {review.status}")
                else:
                    logger.info(f"Skipping AI review for PR action '{action}' on PR {pr.id}")
            except Repository.DoesNotExist:
                logger.warning(f"Repository {repo_full_name} not found in DB. Cannot process PR event.")
        
        elif event_type == 'push':
            logger.info("Push event received. Processing... ")
            repo_data = event_data.get('repository', {})
            repo_full_name = repo_data.get('full_name')
            commits_data = event_data.get('commits', [])

            if not repo_full_name or not commits_data:
                logger.error(f"Missing repo_full_name or commits_data for push event. Data: {event_data}")
                return
            
            try:
                repo = Repository.objects.get(repo_name=repo_full_name)
                for commit_payload in commits_data:
                    commit_sha = commit_payload.get('id')
                    if not commit_sha:
                        logger.warning(f"Skipping commit with no SHA in push event: {commit_payload}")
                        continue
                    
                    commit_author_github_id = str(commit_payload.get('author', {}).get('id') or commit_payload.get('author', {}).get('name'))
                    commit_author_login = commit_payload.get('author', {}).get('username') or commit_payload.get('author', {}).get('name')

                    db_commit, commit_created = Commit.objects.update_or_create(
                        repository=repo,
                        commit_hash=commit_sha,
                        defaults={
                            'author_github_id': commit_author_github_id,
                            'message': commit_payload.get('message'),
                            'author_name': commit_payload.get('author', {}).get('name'),
                            'author_email': commit_payload.get('author', {}).get('email'),
                            'timestamp': commit_payload.get('timestamp'),
                            'url': commit_payload.get('url')
                        }
                    )
                    if commit_created:
                        logger.info(f"Commit {commit_sha:.7} for repo {repo_full_name} CREATED in DB.")
                    else:
                        logger.info(f"Commit {commit_sha:.7} for repo {repo_full_name} UPDATED in DB.")

                    logger.info(f"Commit {db_commit.commit_sha[:7]} processed. AI review for standalone commits via push not auto-triggered by default.")

            except Repository.DoesNotExist:
                logger.warning(f"Repository {repo_full_name} not found in DB. Cannot process push event.")
        else:
            logger.info(f"Webhook event type '{event_type}' not configured for detailed processing.")

    except Exception as e:
        logger.error(f"Error in top-level process_webhook_event task: {str(e)}", exc_info=True)

@shared_task(bind=True)
def process_pr_review(self, event_data: Dict[str, Any], repository_id: int, pr_model_id: int,triggering_user_id: int = None) -> None:
    logger.info(f"PROCESS_PR_REVIEW_TASK: Starting for PR ID {pr_model_id}, Repo ID {repository_id}")
    review = None
    
    # Create a new event loop for this task execution
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        repo = Repository.objects.get(id=repository_id)
        pr = PullRequest.objects.get(id=pr_model_id, repository=repo)

        review, created = Review.objects.get_or_create(
            repository=repo, pull_request=pr, status='pending',
            defaults={'status': 'in_progress', 'review_data': {'message': 'Review picked up by Celery task.'}}
        )
        if not created and review.status == 'pending':
            review.status = 'in_progress'
            review.save(update_fields=['status'])
        elif review.status != 'in_progress':
            logger.warning(f"PROCESS_PR_REVIEW_TASK: Review {review.id} for PR {pr.id} is not 'pending' or 'in_progress' (current: {review.status}). Skipping.")
            return

        logger.info(f"PROCESS_PR_REVIEW_TASK: Processing review {review.id} for PR {pr.id}")

        client = LangGraphClient()
        # Run the async initialize method
        loop.run_until_complete(client.initialize())
        
        if not client.review_agent:
            logger.error("PROCESS_PR_REVIEW_TASK: LangGraph review agent not available after initialization.")
            raise Exception("LangGraph review agent not available.")

        pr_github_payload = event_data.get('pull_request', {})
        if not pr_github_payload:
            logger.error(f"PROCESS_PR_REVIEW_TASK: Missing 'pull_request' data in event_data for review {review.id}")
            raise ValueError("Pull request data missing from webhook event for LangGraph")

        repo_settings = {
            'coding_standards': repo.coding_standards or [],
            'code_metrics': repo.code_metrics or [],
            'llm_preference': repo.llm_preference or settings.DEFAULT_LLM_MODEL,
        }
        pr_author_github_id = str(pr_github_payload.get('user', {}).get('id'))
        pr_author_login = pr_github_payload.get('user', {}).get('login', 'unknown_user')

        logger.info(f"PROCESS_PR_REVIEW_TASK: Calling LangGraph to generate review for review ID {review.id}")
         # Run the async generate_review method
        review_result = loop.run_until_complete(client.generate_review(
            pr_data=pr_github_payload,
            repo_settings=repo_settings,
            user_id=pr_author_github_id 
        ))
        logger.info(f"PROCESS_PR_REVIEW_TASK: LangGraph review generated for review ID {review.id}")

        raw_review_data = review_result.get('review_data', {})
        allowed_review_keys = ["repo", "user", "fixes", "metrics", "reviews", "llm_model", "standards",'final_result']
        filtered_review_data = {key: raw_review_data[key] for key in allowed_review_keys if key in raw_review_data}
        
        # Preserve workspace information in the review record for filtering
        if 'workspace_path' in event_data:
            filtered_review_data['workspace_path'] = event_data['workspace_path']
        if 'repository_name' in event_data:
            filtered_review_data['repository_name'] = event_data['repository_name']
        if 'git_remote_url' in event_data:
            filtered_review_data['git_remote_url'] = event_data['git_remote_url']
        if 'git_branch' in event_data:
            filtered_review_data['git_branch'] = event_data['git_branch']
        if 'is_git_repo' in event_data:
            filtered_review_data['is_git_repo'] = event_data['is_git_repo']
        if 'files_count' in event_data:
            filtered_review_data['files_count'] = len(event_data.get('files', {}))
        if 'total_size_bytes' in event_data:
            total_size = sum(len(str(content).encode('utf-8')) for content in event_data.get('files', {}).values())
            filtered_review_data['total_size_bytes'] = total_size
        
        # Add review timestamp
        filtered_review_data['review_timestamp'] = timezone.now().isoformat()
        
        review.review_data = filtered_review_data
        review.status = 'completed'
        review.save()
        logger.info(f"PROCESS_PR_REVIEW_TASK: Review {review.id} updated and saved as completed.")

        # Create a main thread for this review
        thread_id = review_result.get('thread_id') or uuid.uuid4().hex  # Use a UUID if no thread_id provided
        if thread_id:
            Thread.objects.create(
                review=review,
                thread_id=thread_id,
                thread_type='main',
                title='Initial AI Review',
                status='open'
            )
            logger.info(f"PROCESS_PR_REVIEW_TASK: Created main thread for review {review.id}")

        token_usage_data = review_result.get('token_usage', {})
        if token_usage_data:
            user_for_llm_usage = None
            if triggering_user_id:
                try:
                    user_for_llm_usage = User.objects.get(id=triggering_user_id)
                    logger.info(f"PROCESS_PR_REVIEW_TASK: LLMUsage will be attributed to triggering user ID: {triggering_user_id}")
                except User.DoesNotExist:
                    logger.warning(f"PROCESS_PR_REVIEW_TASK: Triggering user with ID {triggering_user_id} not found. Falling back to PR author for LLMUsage.")

            if not user_for_llm_usage: # Fallback to PR author
                logger.info(f"PROCESS_PR_REVIEW_TASK: LLMUsage will be attributed to PR author GitHub ID: {pr_author_github_id}")
                user_for_llm_usage, _ = User.objects.get_or_create(
                    github_id=pr_author_github_id,
                    defaults={
                        'username': pr_author_login,
                        'email': pr_github_payload.get('user', {}).get('email') # Ensure your User model handles potentially null email
                    }
                )
            
            LLMUsage.objects.create(
                review=review, user=user_for_llm_usage,
                llm_model=repo_settings['llm_preference'],
                input_tokens=token_usage_data.get('prompt_tokens', 0),
                output_tokens=token_usage_data.get('completion_tokens', 0),
                cost=calculate_cost(token_usage_data, repo_settings['llm_preference'])
            )
            logger.info(f"PROCESS_PR_REVIEW_TASK: LLM usage recorded for review {review.id} by user {user_for_llm_usage.username}.")

        github_service = GitHubService() 
        review_url = f"{settings.FRONTEND_URL}/reviews/{review.id}"
        comment_body = (
            f"🤖 AI Code Review Complete!\n\n"
            f"Status: {review.status}\n"
            f"View the full report: {review_url}\n"
            f"(Review ID: {review.id})"
        )

        logger.info(f"PROCESS_PR_REVIEW_TASK: Posting comment to GitHub PR {pr.pr_number} in repo {repo.repo_name}")
        owner_login, repo_name = repo.repo_name.split('/')
        # Run async post_pr_comment
        # Only works if a reviewer requests a re-review
        # loop.run_until_complete(github_service.post_pr_comment(
        #     owner_login=owner_login,
        #     repo_name=repo_name,
        #     pr_number=pr.pr_number,
        #     body=comment_body
        # ))
        # logger.info(f"PROCESS_PR_REVIEW_TASK: Comment posted to GitHub for review {review.id}")

    except PullRequest.DoesNotExist:
        logger.error(f"PROCESS_PR_REVIEW_TASK: PullRequest ID {pr_model_id} not found for repo {repository_id}.")
    except Repository.DoesNotExist:
        logger.error(f"PROCESS_PR_REVIEW_TASK: Repository ID {repository_id} not found.")
    except Exception as e:
        task_id = self.request.id if self.request else "N/A"
        logger.error(f"PROCESS_PR_REVIEW_TASK: Unhandled error in task {task_id} for Review ID {review.id if review else 'N/A'}: {str(e)}", exc_info=True)
        if review and review.status != 'completed':
            review.status = 'failed'
            review.error_message = str(e)[:1023]
            review.save(update_fields=['status', 'error_message'])
        raise
    finally:
        # Ensure the loop is closed
        loop.close()
        asyncio.set_event_loop(None) # Clear the event loop for the current thread
        
@shared_task(bind=True)
def process_commit_review(self, event_data: Dict[str, Any], repository_id: int, commit_model_id: int) -> None:
    """
    Process an AI review for a standalone commit.
    
    Args:
        event_data: Dictionary of GitHub webhook data or manually prepared data
        repository_id: ID of the Repository model instance
        commit_model_id: ID of the Commit model instance
    """
    logger.info(f"PROCESS_COMMIT_REVIEW_TASK: Starting for Commit ID {commit_model_id}, Repo ID {repository_id}")
    review = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        repo = Repository.objects.get(id=repository_id)
        commit = Commit.objects.get(id=commit_model_id, repository=repo)
        
        # Get or create a pending review
        review, created = Review.objects.get_or_create(
            repository=repo,
            commit=commit,
            status='pending',
            defaults={'status': 'in_progress', 'review_data': {'message': 'Commit review picked up by Celery task.'}}
        )
        if not created and review.status == 'pending':
            review.status = 'in_progress'
            review.save(update_fields=['status'])
        elif review.status != 'in_progress':
            logger.warning(f"PROCESS_COMMIT_REVIEW_TASK: Review {review.id} for Commit {commit.id} is not 'pending' or 'in_progress' (current: {review.status}). Skipping.")
            return
        
        logger.info(f"PROCESS_COMMIT_REVIEW_TASK: Processing review {review.id} for Commit {commit.id}")
        
        # Initialize LangGraph client
        client = LangGraphClient()
        loop.run_until_complete(client.initialize())
        
        if not client.review_agent:
            logger.error("PROCESS_COMMIT_REVIEW_TASK: LangGraph review agent not available after initialization.")
            raise Exception("LangGraph review agent not available.")
        
        # Prepare commit data for LangGraph
        commit_github_data = event_data.get('commit', {})
        if not commit_github_data:
            # If not provided in event_data, construct from our DB model
            commit_github_data = {
                'sha': commit.commit_hash,
                'message': commit.message,
                'url': commit.url,
                'author': {
                    'name': getattr(commit, 'author_name', None),
                    'email': getattr(commit, 'author_email', None),
                    'date': commit.timestamp.isoformat() if commit.timestamp else None
                },
                'committer': {
                    'name': getattr(commit, 'committer_name', None),
                    'email': getattr(commit, 'committer_email', None)
                }
            }
        
        # If we need to fetch more detailed commit data from GitHub
        # Here we could use GitHubService to get more info if needed
        
        # Get repo settings
        repo_settings = {
            'coding_standards': repo.coding_standards or [],
            'code_metrics': repo.code_metrics or [],
            'llm_preference': repo.llm_preference or settings.DEFAULT_LLM_MODEL,
        }
        
        # Author identification for LLM usage tracking
        commit_author_github_id = commit.author_github_id
        commit_author_name = getattr(commit, 'author_name', 'unknown_user')
        
        # Generate review using LangGraph
        logger.info(f"PROCESS_COMMIT_REVIEW_TASK: Calling LangGraph to generate review for commit review ID {review.id}")
        
        # Transform the commit data to match what LangGraph expects
        # The structure may need adjustment based on your LangGraph agent's expectations
        input_data = {
            'commit': commit_github_data,
            'repository': {
                'id': repo.github_native_id,
                'full_name': repo.repo_name,
                'owner': {'login': repo.owner.username}
            },
            'commit_sha': commit.commit_hash
        }
        review_result = loop.run_until_complete(
            client.generate_review(
                pr_data=input_data,  # We reuse the PR review function but with commit data
                repo_settings=repo_settings,
                user_id=repo.owner.github_id if repo.owner else None,)
        )
        logger.info(f"PROCESS_COMMIT_REVIEW_TASK: LangGraph review generated for review ID {review.id}")
        
        raw_review_data = review_result.get('review_data', {})
        allowed_review_keys = ["repo", "user", "fixes", "metrics", "reviews", "llm_model", "standards",'final_result']
        filtered_review_data = {key: raw_review_data[key] for key in allowed_review_keys if key in raw_review_data}
        
        review.review_data = filtered_review_data
        review.status = 'completed'
        review.save()
        logger.info(f"PROCESS_COMMIT_REVIEW_TASK: Review {review.id} updated and saved as completed.")
        
        # Create a main thread for this review
        thread_id = review_result.get('thread_id')
        if thread_id:
            Thread.objects.create(
                review=review,
                thread_id=thread_id,
                thread_type='main',
                title='Initial Commit AI Review',
                status='open'
            )
            logger.info(f"PROCESS_COMMIT_REVIEW_TASK: Created main thread for review {review.id}")
        
        # Record token usage
        token_usage_data = review_result.get('token_usage', {})
        if token_usage_data:
            author_user, _ = User.objects.get_or_create(
                github_id=repo.owner.github_id if repo.owner.github_id else f"unknown_{commit_author_name}",
                defaults={
                    'username': commit_author_name,
                    'email': getattr(commit, 'author_email', None)
                }
            )
            LLMUsage.objects.create(
                review=review,
                user=author_user,
                llm_model=repo_settings['llm_preference'],
                input_tokens=token_usage_data.get('input_tokens', 0),
                output_tokens=token_usage_data.get('output_tokens', 0),
                cost=calculate_cost(token_usage_data, repo_settings['llm_preference'])
            )
            logger.info(f"PROCESS_COMMIT_REVIEW_TASK: LLM usage recorded for review {review.id}")
        
        # Post a comment to GitHub if possible
        github_service = GitHubService()
        review_url = f"{settings.FRONTEND_URL}/reviews/{review.id}"
        comment_body = (
            f"🤖 AI Code Review Complete for Commit {commit.commit_hash[:7]}!\n\n"
            f"Status: {review.status}\n"
            f"View the full report: {review_url}\n"
            f"(Review ID: {review.id})"
        )
        
        # try:
        #     logger.info(f"PROCESS_COMMIT_REVIEW_TASK: Posting comment to GitHub commit {commit.commit_hash} in repo {repo.repo_name}")
        #     owner_login, repo_name = repo.repo_name.split('/')
        #     loop.run_until_complete(
        #         github_service.post_commit_comment(
        #             owner_login=owner_login,
        #             repo_name=repo_name,
        #             commit_sha=commit.commit_hash,
        #             body=comment_body
        #         )
        #     )
        #     logger.info(f"PROCESS_COMMIT_REVIEW_TASK: Comment posted to GitHub for review {review.id}")
        # except Exception as e:
        #     logger.error(f"PROCESS_COMMIT_REVIEW_TASK: Failed to post GitHub comment: {str(e)}", exc_info=True)
        #     # We continue even if comment posting fails - the review is still available in our system
    
    except Commit.DoesNotExist:
        logger.error(f"PROCESS_COMMIT_REVIEW_TASK: Commit ID {commit_model_id} not found for repo {repository_id}.")
    except Repository.DoesNotExist:
        logger.error(f"PROCESS_COMMIT_REVIEW_TASK: Repository ID {repository_id} not found.")
    except Exception as e:
        task_id = self.request.id if self.request else "N/A"
        logger.error(f"PROCESS_COMMIT_REVIEW_TASK: Unhandled error in task {task_id} for Review ID {review.id if review else 'N/A'}: {str(e)}", exc_info=True)
        if review and review.status != 'completed':
            review.status = 'failed'
            review.error_message = str(e)[:1023]
            review.save(update_fields=['status', 'error_message'])
        raise

def calculate_cost(token_usage: Dict[str, int], model: str) -> float:
    """Calculate the cost of token usage based on the model."""
    input_cost_per_token = 0.00001  # Default
    output_cost_per_token = 0.00002 # Default

    model_key = model.lower()
    PRICING = {
        "gpt-4": {"input": 0.00003, "output": 0.00006},
        "cerebras::llama-3.3-70b": {"input": 0.0000026, "output": 0.0000035}, # Made up, adjust
        "default": {"input": 0.00001, "output": 0.00002}
    }

    for key_part in PRICING:
        if key_part in model_key:
            input_cost_per_token = PRICING[key_part]["input"]
            output_cost_per_token = PRICING[key_part]["output"]
            break
    
    input_tokens = token_usage.get('input_tokens', 0) or 0
    output_tokens = token_usage.get('output_tokens', 0) or 0
    
    input_cost = input_tokens * input_cost_per_token
    output_cost = output_tokens * output_cost_per_token
    
    return round(input_cost + output_cost, 6) 

@shared_task(bind=True)
def process_vscode_review_task(self, review_id, review_data):
    """
    Process VS Code review asynchronously with real-time updates.
    
    Args:
        review_id: ID of the review record
        review_data: Dictionary containing review parameters
    """
    channel_layer = get_channel_layer()
    review = None
    
    # Create a new event loop for this task execution (same as PR/commit reviews)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Get the review record
        review = Review.objects.get(id=review_id)
        user = review.created_by
        
        logger.info(f"Processing VS Code review {review_id} for user {user.username}")
        
        # Send progress update
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f'user_{user.github_id}',
                {
                    'type': 'review_status_update',
                    'review_id': str(review_id),
                    'status': 'processing',
                    'progress': 25
                }
            )
        
        # Initialize LangGraph client (same as PR/commit reviews)
        client = LangGraphClient()
        loop.run_until_complete(client.initialize())
        
        if not client.review_agent:
            logger.error("PROCESS_VSCODE_REVIEW_TASK: LangGraph review agent not available after initialization.")
            raise Exception("LangGraph review agent not available.")
        
        # Send progress update
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f'user_{user.github_id}',
                {
                    'type': 'review_status_update',
                    'review_id': str(review_id),
                    'status': 'processing',
                    'progress': 50
                }
            )
        
        # Prepare VS Code review data for LangGraph (similar to PR/commit format)
        vscode_review_data = {
            'files': review_data.get('files', {}),
            'diff_str': review_data.get('diff_str', ''),
            'review_type': 'vscode',
            'review_id': str(review_id),  # Add review_id that the agent expects
            'user_id': str(user.github_id),  # Add user_id that the agent expects
            'user': {'login': user.username, 'id': user.github_id},
            'repository': {'full_name': 'vscode-extension/review'},  # Placeholder for VS Code reviews
            # Add additional fields that the LangGraph agent expects
            'temperature': max(0.0, min(1.0, float(review_data.get('temperature', 0.3)))),
            'max_tokens': max(1, min(100000, int(review_data.get('max_tokens', 32768)))),
            'max_tool_calls': max(1, min(20, int(review_data.get('max_tool_calls', 7)))),
            'user_github_token': user.github_access_token if hasattr(user, 'github_access_token') else None,
        }
        
        # Prepare repo settings (similar to PR/commit reviews)
        repo_settings = {
            'coding_standards': review_data.get('standards', []),
            'code_metrics': review_data.get('metrics', []),
            'llm_preference': review_data.get('llm_model', settings.DEFAULT_LLM_MODEL if hasattr(settings, 'DEFAULT_LLM_MODEL') else 'CEREBRAS::llama-3.3-70b'),
            'temperature': max(0.0, min(1.0, float(review_data.get('temperature', 0.3)))),
            'max_tokens': max(1, min(100000, int(review_data.get('max_tokens', 32768)))),
            'max_tool_calls': max(1, min(20, int(review_data.get('max_tool_calls', 7)))),
        }
        
        logger.info(f"PROCESS_VSCODE_REVIEW_TASK: Calling LangGraph to generate review for review ID {review_id}")
        
        # Process the review using LangGraph client (same as PR/commit reviews)
        review_result = loop.run_until_complete(client.generate_review(
            pr_data=vscode_review_data,
            repo_settings=repo_settings,
            user_id=str(user.github_id)
        ))
        
        logger.info(f"PROCESS_VSCODE_REVIEW_TASK: LangGraph review generated for review ID {review_id}")
        
        # Send progress update
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f'user_{user.github_id}',
                {
                    'type': 'review_status_update',
                    'review_id': str(review_id),
                    'status': 'processing',
                    'progress': 75
                }
            )
        
        # Update review with results (same as PR/commit reviews)
        raw_review_data = review_result.get('review_data', {})
        allowed_review_keys = ["repo", "user", "fixes", "metrics", "reviews", "llm_model", "standards", 'final_result']
        filtered_review_data = {key: raw_review_data[key] for key in allowed_review_keys if key in raw_review_data}
        
        # Preserve workspace information in the review record for filtering
        if 'workspace_path' in review_data:
            print(review_data['workspace_path'])
            filtered_review_data['workspace_path'] = review_data['workspace_path']
        if 'repository_name' in review_data:
            print(review_data['repository_name'])
            filtered_review_data['repository_name'] = review_data['repository_name']
        if 'git_remote_url' in review_data:
            print(review_data['git_remote_url'])
            filtered_review_data['git_remote_url'] = review_data['git_remote_url']
        if 'git_branch' in review_data:
            print(review_data['git_branch'])
            filtered_review_data['git_branch'] = review_data['git_branch']
        if 'is_git_repo' in review_data:
            print(review_data['is_git_repo'])
            filtered_review_data['is_git_repo'] = review_data['is_git_repo']
        if 'files_count' in review_data:
            print(review_data['files_count'])
            filtered_review_data['files_count'] = len(review_data.get('files', {}))
        if 'total_size_bytes' in review_data:
            print(review_data['total_size_bytes'])
            total_size = sum(len(str(content).encode('utf-8')) for content in review_data.get('files', {}).values())
            filtered_review_data['total_size_bytes'] = total_size
        
        # Add review timestamp
        filtered_review_data['review_timestamp'] = timezone.now().isoformat()
        
        review.review_data = filtered_review_data
        review.status = 'completed'
        review.thread_id = review_result.get('thread_id')
        review.save()
        
        # Create a main thread for this review (same as PR/commit reviews)
        thread_id = review_result.get('thread_id')
        if thread_id:
            Thread.objects.create(
                review=review,
                thread_id=thread_id,
                thread_type='main',
                title='Initial VS Code AI Review',
                status='open'
            )
            logger.info(f"PROCESS_VSCODE_REVIEW_TASK: Created main thread for review {review_id}")
        
        # Record LLM usage (same as PR/commit reviews)
        token_usage_data = review_result.get('token_usage', {})
        if token_usage_data:
            LLMUsage.objects.create(
                review=review,
                user=user,
                llm_model=repo_settings['llm_preference'],
                input_tokens=token_usage_data.get('input_tokens', token_usage_data.get('prompt_tokens', 0)),
                output_tokens=token_usage_data.get('output_tokens', token_usage_data.get('completion_tokens', 0)),
                cost=calculate_cost(token_usage_data, repo_settings['llm_preference'])
            )
            logger.info(f"PROCESS_VSCODE_REVIEW_TASK: LLM usage recorded for review {review_id} by user {user.username}.")
        
        # Send completion notification
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f'user_{user.github_id}',
                {
                    'type': 'review_status_update',
                    'review_id': str(review_id),
                    'status': 'completed',
                    'progress': 100
                }
            )
            
            # Send detailed completion data
            async_to_sync(channel_layer.group_send)(
                f'review_{review_id}',
                {
                    'type': 'review_completed',
                    'review_data': filtered_review_data,
                    'token_usage': token_usage_data,
                    'thread_id': thread_id
                }
            )
        
        logger.info(f"VS Code review {review_id} completed successfully")
        
        return {
            'status': 'completed',
            'review_id': review_id,
            'review_data': filtered_review_data,
            'token_usage': token_usage_data
        }
        
    except Review.DoesNotExist:
        error_msg = f"Review {review_id} not found"
        logger.error(error_msg)
        return {'status': 'error', 'message': error_msg}
        
    except Exception as e:
        task_id = self.request.id if self.request else "N/A"
        error_msg = f"VS Code review {review_id} failed: {str(e)}"
        logger.error(f"PROCESS_VSCODE_REVIEW_TASK: Unhandled error in task {task_id} for Review ID {review_id}: {str(e)}", exc_info=True)
        
        try:
            # Update review status to failed
            if review:
                review.status = 'failed'
                review.error_message = str(e)[:1023]
                review.save(update_fields=['status', 'error_message'])
            
            # Send error notification
            if channel_layer and review:
                async_to_sync(channel_layer.group_send)(
                    f'user_{review.created_by.github_id}',
                    {
                        'type': 'review_status_update',
                        'review_id': str(review_id),
                        'status': 'failed',
                        'progress': 0
                    }
                )
                
                async_to_sync(channel_layer.group_send)(
                    f'review_{review_id}',
                    {
                        'type': 'review_error',
                        'error': str(e),
                        'message': 'Review processing failed'
                    }
                )
        except Exception as update_error:
            logger.error(f"Failed to update review status: {str(update_error)}")
        
        return {'status': 'error', 'message': error_msg}
        
    finally:
        # Ensure the loop is closed (same as PR/commit reviews)
        loop.close()
        asyncio.set_event_loop(None)