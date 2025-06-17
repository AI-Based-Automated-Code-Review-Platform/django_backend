from rest_framework import serializers
from .models import User, Repository as DBRepository, RepoCollaborator, PullRequest, Commit, Review, Thread, Comment, LLMUsage, ReviewFeedback, WebhookEventLog

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'is_admin', 'github_id', 'avatar_url', 'created_at', 'updated_at']
        read_only_fields = ['id', 'github_id', 'created_at', 'updated_at']

class RepositorySerializer(serializers.ModelSerializer):
    owner = UserSerializer(read_only=True)
    # For request (create/update), owner will be set from the request user, not from input data.
    # For response, owner will be serialized.

    # webhook_url will be generated on creation, can be read_only for updates unless explicitly allowed.
    webhook_url = serializers.CharField(read_only=True, allow_null=True)
    webhook_secret = serializers.CharField(read_only=True)

    class Meta:
        model = DBRepository
        fields = [
            'id', 'owner', 'repo_name', 'repo_url', 'description', 'github_native_id',
            'coding_standards', 'code_metrics', 'llm_preference', 'webhook_url',
            'created_at', 'updated_at', 'webhook_last_event_at', 'webhook_secret'
        ]
        read_only_fields = ['id', 'owner', 'created_at', 'updated_at', 'webhook_url', 'webhook_secret', 'webhook_last_event_at']

    def create(self, validated_data):
        # The owner is set in the view from request.user
        # Webhook URL is also generated in the view after initial save
        return super().create(validated_data)
    
    def validate_repo_name(self, value):
        if '/' not in value or len(value.split('/')) != 2:
            raise serializers.ValidationError("repo_name must be in the format 'owner/repo'.")
        return value

class RepoCollaboratorSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    repo_id = serializers.PrimaryKeyRelatedField(queryset=DBRepository.objects.all(), source='repository')

    class Meta:
        model = RepoCollaborator
        fields = ['id', 'repo_id', 'user', 'role', 'created_at', 'updated_at']
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']

class GitHubRepositorySerializer(serializers.Serializer):
    """Serializer for GitHub repo data that might not directly map to our model fields yet."""
    id = serializers.IntegerField() # GitHub's native ID
    name = serializers.CharField()
    full_name = serializers.CharField()
    private = serializers.BooleanField()
    html_url = serializers.URLField()
    description = serializers.CharField(allow_null=True, required=False)
    owner_login = serializers.CharField(source='owner.login') # Example of accessing nested data
    permissions = serializers.DictField(child=serializers.BooleanField(), required=False) # e.g. {"admin": true, "push": true, "pull": true}
    is_registered_in_system = serializers.BooleanField(default=False)
    system_id = serializers.IntegerField(allow_null=True, required=False)

class GitHubOrganizationSerializer(serializers.Serializer):
    """Serializer for GitHub organization data."""
    login = serializers.CharField()
    id = serializers.IntegerField()
    node_id = serializers.CharField()
    url = serializers.URLField()
    repos_url = serializers.URLField()
    events_url = serializers.URLField()
    hooks_url = serializers.URLField()
    issues_url = serializers.URLField()
    members_url = serializers.URLField()
    public_members_url = serializers.URLField()
    avatar_url = serializers.URLField()
    description = serializers.CharField(allow_blank=True, allow_null=True, required=False)

class GitHubCollaboratorSerializer(serializers.Serializer):
    """Serializer for GitHub collaborator data from the GitHub API."""
    login = serializers.CharField()
    id = serializers.IntegerField()
    avatar_url = serializers.URLField()
    html_url = serializers.URLField()
    type = serializers.CharField() # User or Organization
    site_admin = serializers.BooleanField()
    permissions = serializers.DictField(child=serializers.BooleanField()) # e.g. {"pull": true, "push": true, "admin": false}

# New Serializers
class PRSerializer(serializers.ModelSerializer):
    repository_id = serializers.PrimaryKeyRelatedField(
        queryset=DBRepository.objects.all(), source='repository', write_only=True
    )
    repository = RepositorySerializer(read_only=True) # For displaying repository details
    source = serializers.CharField(read_only=True, required=False)
    # Fields from GitHub API that are not directly on the model but useful for client
    user_login = serializers.CharField(read_only=True, required=False)
    user_avatar_url = serializers.URLField(read_only=True, required=False)
    created_at_gh = serializers.DateTimeField(read_only=True, required=False)
    updated_at_gh = serializers.DateTimeField(read_only=True, required=False)
    closed_at_gh = serializers.DateTimeField(read_only=True, required=False, allow_null=True)
    merged_at_gh = serializers.DateTimeField(read_only=True, required=False, allow_null=True)

    class Meta:
        model = PullRequest
        fields = [
            'id', 'repository', 'repository_id', # Standard fields
            'pr_github_id', 'pr_number', 'title', 'body', 'author_github_id', 
            'status', 'url', 'head_sha', 'base_sha', # Model fields
            'user_login', 'user_avatar_url', # Additional GitHub data
            'created_at_gh', 'updated_at_gh', 'closed_at_gh', 'merged_at_gh', # Additional GitHub data
            'created_at', 'updated_at', # Timestamps from TimestampMixin
            'source'
        ]
        read_only_fields = [
            'id', 'repository', 'created_at', 'updated_at', 'source',
            'user_login', 'user_avatar_url', 'created_at_gh', 'updated_at_gh', 
            'closed_at_gh', 'merged_at_gh'
        ]
    def to_representation(self, instance):
        """
        Augment the representation with non-model fields from initial_data
        when the serializer was initialized with `data=...`.
        """
        representation = super().to_representation(instance)

        # `self.initial_data` holds the original data passed to `data=`
        # `instance` here would be `validated_data` if initialized with `data=`
        if hasattr(self, 'initial_data') and self.initial_data:
            non_model_fields = [
                'user_login', 'user_avatar_url', 'created_at_gh',
                'updated_at_gh', 'closed_at_gh', 'merged_at_gh'
            ]
            for field_name in non_model_fields:
                if field_name in self.initial_data:
                    representation[field_name] = self.initial_data[field_name]
        
        # Set 'source' from context if provided, otherwise ensure it's present (e.g. as None)
        # if not already set by super() from a model field (which it isn't for 'source').
        if self.context.get('source'):
            representation['source'] = self.context.get('source')
        elif 'source' not in representation: # Default if not set by super and not in context
            representation['source'] = None

        return representation
class CommitSerializer(serializers.ModelSerializer):
    repository_id = serializers.PrimaryKeyRelatedField(
        queryset=DBRepository.objects.all(), source='repository', write_only=True
    )
    repository = RepositorySerializer(read_only=True) # For displaying repository details
    source = serializers.CharField(read_only=True, required=False)
    # Fields from GitHub API that are not directly on the model but useful for client
    author_name = serializers.CharField(read_only=True, required=False)
    author_email = serializers.EmailField(read_only=True, required=False)
    # author_date = serializers.DateTimeField(read_only=True, required=False) # Covered by model's timestamp
    committer_name = serializers.CharField(read_only=True, required=False, allow_null=True)
    committer_email = serializers.EmailField(read_only=True, required=False, allow_null=True)
    committed_date = serializers.DateTimeField(read_only=True, required=False, allow_null=True)


    class Meta:
        model = Commit
        fields = [
            'id', 'repository', 'repository_id', # Standard fields
            'commit_hash', 'message', 'author_github_id', 'committer_github_id', 
            'url', 'timestamp', # Model fields
            'author_name', 'author_email', # Additional GitHub data
            'committer_name', 'committer_email', 'committed_date', # Additional GitHub data
            'created_at', 'updated_at', # Timestamps from TimestampMixin
            'source'
        ]
        read_only_fields = [
            'id', 'repository', 'created_at', 'updated_at', 'source',
            'author_name', 'author_email', 
            'committer_name', 'committer_email', 'committed_date'
        ]
    def to_representation(self, instance):
        """
        Augment the representation with non-model fields from initial_data
        when the serializer was initialized with `data=...`.
        """
        representation = super().to_representation(instance)

        if hasattr(self, 'initial_data') and self.initial_data:
            non_model_fields = [
                'author_name', 'author_email',
                'committer_name', 'committer_email', 'committed_date'
            ]
            for field_name in non_model_fields:
                if field_name in self.initial_data:
                    representation[field_name] = self.initial_data[field_name]
        
        if self.context.get('source'):
            representation['source'] = self.context.get('source')
        elif 'source' not in representation: # Default if not set by super and not in context
            representation['source'] = None
            
        return representation
class ReviewFeedbackSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True) # Feedback is always by the logged-in user
    review = serializers.PrimaryKeyRelatedField(queryset=Review.objects.all())

    class Meta:
        model = ReviewFeedback
        fields = ['id', 'review', 'user', 'rating', 'feedback', 'created_at', 'updated_at']
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']

    def create(self, validated_data):
        # User is set from the request context in the view
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)

class CommentSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True) # Comment is always by the logged-in user
    thread = serializers.PrimaryKeyRelatedField(queryset=Thread.objects.all())
    parent_comment = serializers.PrimaryKeyRelatedField(queryset=Comment.objects.all(), allow_null=True, required=False)
    # replies = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = [
            'id', 'thread', 'user', 'comment', 'comment_data', 'type', 
            'parent_comment', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'thread', 'created_at', 'updated_at']

    def get_replies(self, obj):
        # Avoids excessively deep nesting or circular dependencies if not careful
        if self.context.get('depth', 0) > 10: # Control nesting depth
            return []
        
        # Create a new context with incremented depth
        new_context = self.context.copy()
        new_context['depth'] = self.context.get('depth', 0) + 1
        
        replies = Comment.objects.filter(parent_comment=obj)
        return CommentSerializer(replies, many=True, read_only=True, context=new_context).data


    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)

class ThreadSerializer(serializers.ModelSerializer):
    review = serializers.PrimaryKeyRelatedField(queryset=Review.objects.all())
    comments = CommentSerializer(many=True, read_only=True) # Nested comments
    comment_count = serializers.SerializerMethodField()
    created_by = UserSerializer(read_only=True) # Comment is always by the logged-in user
    class Meta:
        model = Thread
        fields = ['id', 'review', 'created_by', 'status', 'thread_id', 'title', 'thread_type', 'comments', 'comment_count', 'created_at', 'updated_at', 'last_comment_at']
        read_only_fields = ['id', 'comments', 'comment_count', 'created_at', 'created_by', 'updated_at', 'last_comment_at']

    def get_comment_count(self, obj):
        return obj.comments.count()

    def create(self, validated_data):
        # Review is typically set from the context (e.g., URL in ReviewViewSet.create_thread)
        # or passed in validated_data if creating a thread directly.
        return super().create(validated_data)
    def to_representation(self, instance):
        data = super().to_representation(instance)

        if 'comments' in data and data['comments']:
            comments_data = data['comments']
            for i, comment_dict in enumerate(comments_data):
                # Ensure 'replies' is removed (already handled by CommentSerializer's Meta)
                comment_dict.pop('replies', None)

                if 'comment_data' in comment_dict and comment_dict['comment_data'] is not None:
                    original_comment_data_dict = comment_dict['comment_data']
                    filtered_comment_data = {}
                    
                    if isinstance(original_comment_data_dict, dict):
                        base_fields_to_keep = [
                            'repo', 'user', 'pr_id', 'feedback', 'llm_model', 
                            'standards', 'metrics', 'reviewer_id', 'feedback_status', 
                            'feedback_suggestion', 'feedback_explanation'
                        ]
                        for field in base_fields_to_keep:
                            if field in original_comment_data_dict:
                                filtered_comment_data[field] = original_comment_data_dict[field]

                        # For the last comment's comment_data, include additional fields
                        if i == len(comments_data) - 1:
                            additional_fields_for_last = ['messages', 'original_review', 'updated_review']
                            for field in additional_fields_for_last:
                                if field in original_comment_data_dict:
                                    filtered_comment_data[field] = original_comment_data_dict[field]
                        
                        comment_dict['comment_data'] = filtered_comment_data if filtered_comment_data else None
                    else:
                        # If original_comment_data_dict is not a dict, set to None
                        comment_dict['comment_data'] = None
                # If comment_data was None or not present, it remains as is (None or not present)
        return data
class ReviewSerializer(serializers.ModelSerializer):
    repository_id = serializers.PrimaryKeyRelatedField(
        queryset=DBRepository.objects.all(), source='repository', write_only=True
    )
    pull_request_id = serializers.PrimaryKeyRelatedField(
        queryset=PullRequest.objects.all(), source='pull_request', allow_null=True, required=False, write_only=True
    )
    commit_id = serializers.PrimaryKeyRelatedField(
        queryset=Commit.objects.all(), source='commit', allow_null=True, required=False, write_only=True
    )
    parent_review_id = serializers.PrimaryKeyRelatedField(
        queryset=Review.objects.all(), source='parent_review', allow_null=True, required=False, write_only=True
    )

    # For read operations, use nested serializers or string representations
    repository = RepositorySerializer(read_only=True)
    pull_request = PRSerializer(read_only=True)
    commit = CommitSerializer(read_only=True)
    parent_review = serializers.PrimaryKeyRelatedField(read_only=True)
    
    # Add threads relationship
    threads = ThreadSerializer(many=True, read_only=True)
    thread_count = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = [
            'id', 
            'repository', 'repository_id', 
            'pull_request', 'pull_request_id',
            'commit', 'commit_id', 
            'parent_review', 'parent_review_id',
            'status', 'review_data', 'threads', 'thread_count', 'error_message',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'repository', 'pull_request', 'commit', 'parent_review', 'threads', 'thread_count']
        extra_kwargs = {
            'status': {'required': False},  # Often auto-set
            'review_data': {'required': False},  # Often set by system processes
        }
    
    def get_thread_count(self, obj):
        return obj.threads.count()
    def to_representation(self, instance):
        data = super().to_representation(instance)
        
        # Include threads with comments if requested
        if self.context.get('include_threads'):
            thread_serializer = ThreadSerializer(
                instance.threads.all(),
                many=True,
                context=self.context
            )
            data['threads'] = thread_serializer.data
        
        return data
class LLMUsageSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    review = serializers.PrimaryKeyRelatedField(queryset=Review.objects.all(), allow_null=True, required=False)

    class Meta:
        model = LLMUsage
        fields = ['id', 'user', 'review', 'llm_model', 'input_tokens', 'output_tokens', 'cost', 'created_at', 'updated_at']
        read_only_fields = ['id', 'user', 'created_at', 'updated_at', 'llm_model', 'input_tokens', 'output_tokens', 'cost']

    def create(self, validated_data):
        # If user is not part of the input, set it from context (e.g., request.user)
        if 'user' not in validated_data and self.context.get('request'):
            validated_data['user'] = self.context['request'].user
        return super().create(validated_data)

class AdminUserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['username', 'email', 'is_admin', 'is_staff', 'is_active'] # Fields admin can update
        # Ensure that sensitive fields like password, github_id, github_access_token are not here
class RepositoryMinimalSerializer(serializers.ModelSerializer):
    class Meta:
        model = DBRepository
        fields = ['repo_name', 'repo_url', 'description', 'webhook_url']
# Add other serializers here as we migrate endpoints
class WebhookEventLogSerializer(serializers.ModelSerializer):
    repository = RepositoryMinimalSerializer(read_only=True)
    
    class Meta:
        model = WebhookEventLog
        fields = [
            'id',
            'event_type',
            'status',
            'created_at',
            'error_message',
            'repository',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'repository']