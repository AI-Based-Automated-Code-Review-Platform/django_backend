import logging
import time
import json
from typing import Dict, Any, Optional
from django.conf import settings
from langgraph_sdk import get_client
from langsmith import Client
from ..models import User
logger = logging.getLogger(__name__)

class LangGraphClient:
    def __init__(self):
        self.client = get_client(url=settings.LANGGRAPH_API_URL,timeout=settings.LANGGRAPH_API_TIMEOUT)
        self.assistants = None
        self.review_agent = None
        self.feedback_agent = None

    async def initialize(self):
        """Initialize the client and get assistants"""
        try:
            # Ensure client is initialized (it is in __init__ if url is present)
            # self.assistants = await self.client.assistants.search() # No longer searching all
            
            # Fetch specific assistants by ID or name from settings
            # assistants = await self.client.assistants.search()
            self.review_agent = await self.client.assistants.get(settings.LANGGRAPH_REVIEW_ASSISTANT_ID)
            self.feedback_agent = await self.client.assistants.get(settings.LANGGRAPH_FEEDBACK_ASSISTANT_ID)
            self.langsmith_client = Client(api_key=settings.LANGSMITH_API_KEY)
            if not self.review_agent:
                logger.error(f"Review agent with ID '{settings.LANGGRAPH_REVIEW_ASSISTANT_ID}' not found.")
            if not self.feedback_agent:
                logger.error(f"Feedback agent with ID '{settings.LANGGRAPH_FEEDBACK_ASSISTANT_ID}' not found.")
                
        except Exception as e:
            logger.error(f"Error initializing LangGraph client or fetching assistants: {str(e)}")
            # Depending on policy, you might want to set agents to None or re-raise
            self.review_agent = None
            self.feedback_agent = None
            # raise # Optionally re-raise if assistant presence is critical for startup
    async def _get_user_github_token(self, user_github_id: str) -> Optional[str]:
        """Retrieve the GitHub token for a user"""
        try:
            user = await User.objects.aget(github_id=user_github_id)
            return user.github_access_token
        except User.DoesNotExist:
            logger.warning(f"User with GitHub ID {user_github_id} not found in the database.")
            return None
        except Exception as e:
            logger.error(f"Error retrieving GitHub token for user {user_github_id}: {e}")
            return None
    async def generate_review(
        self,
        pr_data: Dict[str, Any],
        repo_settings: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Generate a code review for a pull request"""
        if not self.review_agent:
            await self.initialize()
        github_token = await self._get_user_github_token(user_id)
        try:
            # Create a new thread for the review
            thread = await self.client.threads.create()
            
            # Determine review type
            is_commit_review = 'commit' in pr_data and pr_data.get('commit') and isinstance(pr_data.get('commit'), dict)
            is_vscode_review = pr_data.get('review_type') == 'vscode' or ('diff_str' in pr_data and 'files' in pr_data)
            # Prepare input data for the review
            input_data = {
                "llm_model": repo_settings.get('llm_preference', 'CEREBRAS::llama-3.3-70b'),
                "standards": repo_settings.get('coding_standards', []),
                "metrics": repo_settings.get('code_metrics', []),
                "temperature": repo_settings.get('temperature', 0.3),
                "max_tokens": repo_settings.get('max_tokens', 32768),
                "max_tool_calls": repo_settings.get('max_tool_calls', 7),
                "user_github_token": github_token
            }
            
            # Handle VS Code review
            if is_vscode_review:
                # VS Code reviews have diff_str and files data
                input_data.update({
                    "files": json.dumps(pr_data.get('files', {})) if isinstance(pr_data.get('files'), dict) else pr_data.get('files', '{}'),
                    "diff_str": pr_data.get('diff_str', ''),
                    "user_id": user_id,
                    "review_id": pr_data.get('review_id', ''),
                    "action": "vscode_review",
                    # For VS Code reviews, we don't have traditional repo/PR structure
                    "user": pr_data.get('user', {}).get('login', ''),
                    "repo": pr_data.get('repository', {}).get('full_name', '').split('/')[-1] if pr_data.get('repository', {}).get('full_name') else 'vscode-review',
                    "pr_id": "",  # Empty for VS Code reviews
                    "commit_hash": "",  # Empty for VS Code reviews
                })
            # Handle PR review
            elif not is_commit_review and 'base' in pr_data:
                input_data.update({
                    "user": pr_data.get('user', {}).get('login', ''),
                    "repo": pr_data.get('base', {}).get('repo', {}).get('name', ''),
                    "pr_id": pr_data.get("number", ""),
                    "commit_hash": "",  # Empty for PR reviews
                })
            # Handle Commit review
            else:
                # For commit reviews (either from the event_data format or our custom format)
                commit_data = pr_data.get('commit', {})
                repo_name = pr_data.get('repository', {}).get('full_name', '')
                
                # Extract commit hash - handle different possible structures
                commit_hash = ""
                if isinstance(commit_data, dict):
                    commit_hash = commit_data.get('sha', commit_data.get('id', ''))
                elif 'commit_sha' in pr_data:
                    commit_hash = pr_data.get('commit_sha', '')
                    
                input_data.update({
                    "user": repo_name.split('/')[0] if repo_name else '',
                    "repo": repo_name.split('/')[1] if '/' in repo_name else repo_name,
                    "pr_id": "",  # Empty for commit reviews
                    "commit_hash": commit_hash,
                })

            # Start the review process
            run = await self.client.runs.create(
                thread_id=thread['thread_id'],
                assistant_id=self.review_agent['assistant_id'],
                input=input_data,
                config={"recursion_limit": 99999999}
            )

            # Wait for the run to complete
            completed_run = await self.client.runs.join(run_id=run['run_id'], thread_id=thread["thread_id"])

            # Get the final state of the feedback
            final_state = await self.client.threads.get_state(thread['thread_id'])
            token_usage = {}
            try:
                time.sleep(5)  # Optional: wait a bit for the run to be fully processed
                meta = self.langsmith_client.read_run(run_id=run['run_id'])
                token_usage = {
                    'input_tokens': meta.prompt_tokens,
                    'output_tokens': meta.completion_tokens,
                    'total_tokens': meta.total_tokens
                }
            except Exception as e_ls:
                logger.error(f"Could not fetch token usage from Langsmith: {e_ls}")
            return {
                'thread_id': thread['thread_id'],
                'run_id': run['run_id'],
                'review_data': final_state['values'],
                'token_usage': token_usage
            }

        except Exception as e:
            logger.error(f"Error generating review: {str(e)}")
            raise

    async def handle_feedback(
        self,
        feedback: str,
        thread_id: str,
        user_id: str,
        is_first_message:bool = False,
        review_data: Optional[Dict[str, Any]] = None,
        repo_settings: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Handle feedback for a review"""
        if not self.feedback_agent:
            await self.initialize()
        github_token = await self._get_user_github_token(user_id)
        try:
            # Prepare input data for feedback
            messages_for_langgraph = []
            # Convert conversation_history to LangGraph message format if necessary
            # For example: messages_for_langgraph = [(msg_role, msg_content) for msg_role, msg_content in conversation_history]
            # Then add the current feedback
            messages_for_langgraph.append(("user", feedback))

            input_data = {
                "messages": messages_for_langgraph, # Use the constructed history + new message
                "feedback": feedback,
                "reviewer_id": user_id, # This should be the ID of the user giving feedback
                "thread_id": thread_id # thread_id is usually part of the run config, not input directly to messages
            }
            if is_first_message:
                input_data.update({
                    "original_review": review_data.get("review_data", {}).get("final_result", {}), # Assuming review_data contains the structure
                    "updated_review": review_data.get("review_data", {}).get("final_result", {}), # Assuming final_result is the updated review
                    "llm_model": repo_settings.get('llm_preference', settings.DEFAULT_LLM_MODEL),
                    "user": review_data.get("repository", {}).get("owner", {}).get("username"), # Example, adjust as per actual data
                    "repo": review_data.get("repository", {}).get("repo_name").split("/")[-1], # Example
                    "user_github_token": github_token, # GitHub token for the user
                    "pr_id": str(review_data.get("pull_request", {}).get("pr_number", "")) if review_data.get("pull_request") else None, # Example
                    "standards": repo_settings.get('coding_standards', []),
                    "metrics": repo_settings.get('code_metrics', []),
                    "temperature": settings.DEFAULT_TEMPERATURE,
                    "max_tokens": settings.DEFAULT_MAX_TOKENS,
                    "max_tool_calls": settings.DEFAULT_MAX_TOOL_CALLS,
                    "reviewer_id": user_id, # Already present
                })
            
            # The thread_id for the run/wait call
            config = {"recursion_limit": 99999999} # Configurable can be added if needed by your LangGraph setup

            run = await self.client.runs.create( # Use create then join, or wait if your SDK version supports it well
                assistant_id=self.feedback_agent['assistant_id'],
                thread_id=thread_id,
                input=input_data,
                config=config # Pass config here if using create
            )
            
            # Wait for the run to complete
            completed_run = await self.client.runs.join(run_id=run['run_id'], thread_id=thread_id) # Adjust timeout

            # Get the final state of the feedback
            final_state = await self.client.threads.get_state(thread_id)
            
            # Fetch token usage if not in completed_run or final_state
            # This part depends on your LangGraph SDK version and Langsmith client integration
            # token_usage = completed_run.get('token_usage', {})
            # if not token_usage and hasattr(self, 'langsmith_client'): # Hypothetical langsmith_client
            token_usage = {}
            try:
                time.sleep(1)  # Optional: wait a bit for the run to be fully processed
                meta = self.langsmith_client.read_run(run_id=run['run_id'])
                token_usage = {
                    'input_tokens': meta.prompt_tokens,
                    'output_tokens': meta.completion_tokens,
                    'total_tokens': meta.total_tokens
                }
            except Exception as e_ls:
                logger.error(f"Could not fetch token usage from Langsmith: {e_ls}")

            return {
                'run_id': run['run_id'],
                'feedback_data': final_state.get('values', {}), # Get the 'values' from the state
                'token_usage': token_usage
            }

        except Exception as e:
            logger.error(f"Error handling feedback: {str(e)}")
            raise 