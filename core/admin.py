from django.contrib import admin
from .models import (
    User, Repository, RepoCollaborator, PullRequest, Commit, 
    Review, Thread, Comment, LLMUsage, ReviewFeedback, WebhookEventLog
)

# Register your models here.

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('username', 'email', 'github_id', 'is_staff', 'is_admin', 'created_at')
    search_fields = ('username', 'email', 'github_id')
    list_filter = ('is_staff', 'is_admin', 'created_at')

@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    list_display = ('repo_name', 'owner', 'repo_url', 'github_native_id', 'created_at')
    search_fields = ('repo_name', 'owner__username', 'repo_url')
    list_filter = ('created_at', 'owner')
    raw_id_fields = ('owner',)

@admin.register(RepoCollaborator)
class RepoCollaboratorAdmin(admin.ModelAdmin):
    list_display = ('repository', 'user', 'role', 'created_at')
    search_fields = ('repository__repo_name', 'user__username', 'role')
    list_filter = ('role', 'created_at')
    raw_id_fields = ('repository', 'user')

@admin.register(PullRequest)
class PullRequestAdmin(admin.ModelAdmin):
    list_display = ('title', 'repository', 'pr_number', 'status', 'author_github_id', 'created_at')
    search_fields = ('title', 'repository__repo_name', 'author_github_id')
    list_filter = ('status', 'created_at')
    raw_id_fields = ('repository',)

@admin.register(Commit)
class CommitAdmin(admin.ModelAdmin):
    list_display = ('commit_hash_short', 'repository', 'message_short', 'author_github_id', 'timestamp')
    search_fields = ('commit_hash', 'repository__repo_name', 'author_github_id')
    list_filter = ('timestamp', 'repository')
    raw_id_fields = ('repository',)

    def message_short(self, obj):
        return obj.message[:50] + '...' if len(obj.message) > 50 else obj.message
    message_short.short_description = 'Message'

    def commit_hash_short(self, obj):
        return obj.commit_hash[:12]
    commit_hash_short.short_description = 'Commit Hash'

@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('id', 'repository', 'pull_request_info', 'commit_info', 'status', 'created_at')
    search_fields = ('repository__repo_name', 'pull_request__title', 'commit__commit_hash')
    list_filter = ('status', 'created_at')
    raw_id_fields = ('repository', 'pull_request', 'commit', 'parent_review')

    def pull_request_info(self, obj):
        if obj.pull_request:
            return f"PR #{obj.pull_request.pr_number} ({obj.pull_request.title[:30]}...)"
        return None
    pull_request_info.short_description = 'Pull Request'

    def commit_info(self, obj):
        if obj.commit:
            return obj.commit.commit_hash[:12]
        return None
    commit_info.short_description = 'Commit'

@admin.register(Thread)
class ThreadAdmin(admin.ModelAdmin):
    list_display = ('id', 'review_info', 'status', 'thread_id_short', 'created_at')
    search_fields = ('review__id', 'thread_id')
    list_filter = ('status', 'created_at')
    raw_id_fields = ('review',)

    def review_info(self, obj):
        return f"Review ID: {obj.review.id}"
    review_info.short_description = 'Review'

    def thread_id_short(self, obj):
        if obj.thread_id:
            return obj.thread_id[:12] + "..." if len(obj.thread_id) > 12 else obj.thread_id
        return None
    thread_id_short.short_description = 'Thread ID'

@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ('id', 'thread_info', 'user', 'type', 'comment_short', 'created_at')
    search_fields = ('thread__id', 'user__username', 'comment')
    list_filter = ('type', 'created_at')
    raw_id_fields = ('thread', 'user', 'parent_comment')

    def thread_info(self, obj):
        return f"Thread ID: {obj.thread.id}"
    thread_info.short_description = 'Thread'
    
    def comment_short(self, obj):
        return obj.comment[:75] + '...' if len(obj.comment) > 75 else obj.comment
    comment_short.short_description = 'Comment'

@admin.register(LLMUsage)
class LLMUsageAdmin(admin.ModelAdmin):
    list_display = ('user', 'review_info', 'llm_model', 'input_tokens', 'output_tokens', 'cost', 'created_at')
    search_fields = ('user__username', 'review__id', 'llm_model')
    list_filter = ('llm_model', 'created_at')
    raw_id_fields = ('user', 'review')

    def review_info(self, obj):
        if obj.review:
            return f"Review ID: {obj.review.id}"
        return "N/A"
    review_info.short_description = 'Review'

@admin.register(ReviewFeedback)
class ReviewFeedbackAdmin(admin.ModelAdmin):
    list_display = ('review_info', 'user', 'rating', 'feedback_short', 'created_at')
    search_fields = ('review__id', 'user__username', 'feedback')
    list_filter = ('rating', 'created_at')
    raw_id_fields = ('review', 'user')

    def review_info(self, obj):
        return f"Review ID: {obj.review.id}"
    review_info.short_description = 'Review'

    def feedback_short(self, obj):
        return obj.feedback[:75] + '...' if len(obj.feedback) > 75 else obj.feedback
    feedback_short.short_description = 'Feedback'

@admin.register(WebhookEventLog)
class WebhookEventLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'event_type', 'status', 'created_at', 'error_message_short')
    search_fields = ('event_type', 'error_message', 'payload') # Added payload to search
    list_filter = ('event_type', 'status', 'created_at')
    readonly_fields = ('payload_pretty', 'created_at', 'updated_at') # Make payload readable

    def error_message_short(self, obj):
        if obj.error_message:
            return obj.error_message[:75] + '...' if len(obj.error_message) > 75 else obj.error_message
        return None
    error_message_short.short_description = 'Error Message'

    def payload_pretty(self, instance):
        import json
        from django.utils.html import format_html
        if instance.payload:
            return format_html("<pre>{}</pre>", json.dumps(instance.payload, indent=2))
        return None
    payload_pretty.short_description = 'Payload (Formatted)'
