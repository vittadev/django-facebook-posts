# -*- coding: utf-8 -*-
from django.db import models
try:
    from django.db.transaction import atomic
except ImportError:
    from django.db.transaction import commit_on_success as atomic
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic
from facebook_api import fields
from facebook_api.utils import graph
from facebook_api.decorators import fetch_all
from facebook_api.models import FacebookGraphIDModel, FacebookGraphManager, MASTER_DATABASE
from facebook_applications.models import Application
from facebook_users.models import User
from facebook_pages.models import Page
from datetime import datetime
import logging
import time
import re

log = logging.getLogger('facebook_posts')

class UnknownResourceType(Exception):
    pass

def get_or_create_from_small_resource(resource):
    '''
    Return instance of right type based on dictionary resource from Facebook API Graph
    '''
    keys = sorted(resource.keys())
    defaults = dict(resource)
    del defaults['id']
    if keys == ['category','id','name']:
        # resource is a page
        return Page.objects.get_or_create(graph_id=resource['id'], defaults=defaults)[0]
    elif keys == ['id','name']:
        # resource is a user
        return User.objects.get_or_create(graph_id=resource['id'], defaults=defaults)[0]
    elif keys == ['id','name','namespace']:
        # resource is a application
        return Application.objects.get_or_create(graph_id=resource['id'], defaults=defaults)[0]
    else:
        raise UnknownResourceType("Resource with strange keys: %s" % keys)

class PostFacebookGraphManager(FacebookGraphManager):

    def update_count_and_get_posts(self, instances, page, *args, **kwargs):
        page.posts_count = page.wall_posts.count()
        page.save()
        return instances

    @atomic
    @fetch_all(return_all=update_count_and_get_posts, always_all=True, paging_next_arg_name='until')
    def fetch_page_wall(self, page, limit=250, offset=0, until=None, since=None, **kwargs):
        '''
        Arguments:
         * until|since - timestamp or datetime
        '''
        kwargs = {
            'limit': int(limit),
            'offset': int(offset),
        }
        for field in ['until', 'since']:
            value = locals()[field]
            if isinstance(value, datetime):
                kwargs[field] = int(time.mktime(value.timetuple()))
            elif value is not None:
                try:
                    kwargs[field] = int(value)
                except TypeError:
                    raise ValueError('Wrong type of argument %s: %s' % (field, type(value)))

        response = graph('%s/posts' % page.graph_id, **kwargs)
        ids = []
        if response:
            log.debug('response objects count - %s' % len(response.data))

            page_ct = ContentType.objects.get_for_model(page)
            if response:
                log.debug('response objects count=%s, limit=%s, after=%s' % (len(response.data), limit, kwargs.get('after')))
                for resource in response.data:
                    instance = Post.remote.get_or_create_from_resource(resource)

                    if instance.owners.using(MASTER_DATABASE).count() == 0:
                        post_owner = PostOwner.objects.get_or_create(post=instance, owner_content_type=page_ct, owner_id=page.pk)[0]
                        instance.owners.add(post_owner)

                    ids += [instance.pk]

        return Post.objects.filter(pk__in=ids), response

class FacebookLikableModel(models.Model):
    class Meta:
        abstract = True

    likes_count = models.IntegerField(default=0)

    def update_count_and_get_like_users(self, instances, *args, **kwargs):
        self.like_users = instances
        self.likes_count = instances.count()
        self.save()
        return instances

    @atomic
    @fetch_all(return_all=update_count_and_get_like_users, paging_next_arg_name='after')
    def fetch_likes(self, limit=1000, **kwargs):
        '''
        Retrieve and save all likes of post
        '''
        ids = []
        response = graph('%s/likes' % self.graph_id, limit=limit, **kwargs)
        if response:
            log.debug('response objects count=%s, limit=%s, after=%s' % (len(response.data), limit, kwargs.get('after')))
            for resource in response.data:
                try:
                    user = get_or_create_from_small_resource(resource)
                    ids += [user.pk]
                except UnknownResourceType:
                    continue

        return User.objects.filter(pk__in=ids), response


class Post(FacebookGraphIDModel, FacebookLikableModel):
    class Meta:
        verbose_name = 'Facebook post'
        verbose_name_plural = 'Facebook posts'

    like_users = models.ManyToManyField(User, related_name='like_posts')

    # in API field called `from`
    author_json = fields.JSONField(null=True, help_text='Information about the user who posted the message') # object containing the name and Facebook id of the user who posted the message
    owners_json = fields.JSONField(null=True, help_text='Profiles mentioned or targeted in this post') # Contains in data an array of objects, each with the name and Facebook id of the user

    author_content_type = models.ForeignKey(ContentType, null=True, related_name='facebook_posts')
    author_id = models.PositiveIntegerField(null=True, db_index=True)
    author = generic.GenericForeignKey('author_content_type', 'author_id')

    application = models.ForeignKey(Application, null=True, help_text='Application this post came from', related_name='posts')

    message = models.TextField(help_text='The message')

    object_id = models.BigIntegerField(null=True, help_text='The Facebook object id for an uploaded photo or video')

    created_time = models.DateTimeField(help_text='The time the post was initially published', db_index=True)
    updated_time = models.DateTimeField(null=True, help_text='The time of the last comment on this post')

    picture = models.TextField(help_text='If available, a link to the picture included with this post')
    source = models.TextField(help_text='A URL to a Flash movie or video file to be embedded within the post')
    link = models.URLField(max_length=1000, help_text='The link attached to this post')
    icon = models.URLField(max_length=500, help_text='A link to an icon representing the type of this post')

    name = models.TextField(help_text='The name of the link')
    type = models.CharField(max_length=100, help_text='A string indicating the type for this post (including link, photo, video)')

    caption = models.TextField(help_text='The caption of the link (appears beneath the link name)')
    description = models.TextField(help_text='A description of the link (appears beneath the link caption)')
    story = models.TextField(help_text='Text of stories not intentionally generated by users, such as those generated when two users become friends; you must have the "Include recent activity stories" migration enabled in your app to retrieve these stories')

    properties = fields.JSONField(null=True, help_text='A list of properties for an uploaded video, for example, the length of the video')
    actions = fields.JSONField(null=True, help_text='A list of available actions on the post (including commenting, liking, and an optional app-specified action)')
    privacy = fields.JSONField(null=True, help_text='The privacy settings of the Post') # object containing the value field and optional friends, networks, allow, deny and description fields.
    place = fields.JSONField(null=True, help_text='Location associated with a Post, if any') # object containing id and name of Page associated with this location, and a location field containing geographic information such as latitude, longitude, country, and other fields (fields will vary based on geography and availability of information)
    message_tags = fields.JSONField(null=True, help_text='Objects tagged in the message (Users, Pages, etc)') # object containing fields whose names are the indexes to where objects are mentioned in the message field; each field in turn is an array containing an object with id, name, offset, and length fields, where length is the length, within the message field, of the object mentioned
    story_tags = fields.JSONField(null=True, help_text='Objects (Users, Pages, etc) tagged in a non-intentional story; you must have the "Include recent activity stories" migration enabled in your app to retrieve these tags') # object containing fields whose names are the indexes to where objects are mentioned in the message field; each field in turn is an array containing an object with id, name, offset, and length fields, where length is the length, within the message field, of the object mentioned
    with_tags = fields.JSONField(null=True, help_text='Objects (Users, Pages, etc) tagged as being with the publisher of the post ("Who are you with?" on Facebook)') # objects containing id and name fields, encapsulated in a data[] array

    likes_json = fields.JSONField(null=True, help_text='Likes for this post') #Structure containing a data object and the count of total likes, with data containing an array of objects, each with the name and Facebook id of the user who liked the post
    comments_json = fields.JSONField(null=True, help_text='Comments for this post') # Structure containing a data object containing an array of objects, each with the id, from, message, and created_time for each comment
    shares_json = fields.JSONField(null=True, help_text='Shares for this post') # Just only get count here, maybe the detail only for manager?

    # not in API
    status_type = models.CharField(max_length=100)
    expanded_height = models.IntegerField(null=True)
    expanded_width = models.IntegerField(null=True)

    # extracted from inner data
    comments_count = models.IntegerField(default=0)

    shares_count = models.IntegerField(default=0)

    objects = models.Manager()
    remote = PostFacebookGraphManager()

    def __unicode__(self):
        return '%s: %s' % (unicode(self.author), self.message or self.story)

    def parse(self, response):

        if 'from' in response:
            response['author_json'] = response.pop('from')
        if 'to' in response and len(response['to']['data']):
            response['owners_json'] = response.pop('to')['data']

        for field in ['likes','comments','shares']:
            if field in response:
                if 'count' in response[field]:
                    response['%s_count' % field] = response[field]['count']
                response['%s_json' % field] = response.pop(field)

        super(Post, self).parse(response)

        if self.author is None and self.author_json:
            self.author = get_or_create_from_small_resource(self.author_json)

        if self.owners.count() == 0 and self.owners_json:
            for owner_json in self.owners_json:
                owner = get_or_create_from_small_resource(owner_json)
                if owner:
                    self._external_links_to_add += [('owners', PostOwner(post=self, owner=owner))]

    def update_count_and_get_comments(self, instances, *args, **kwargs):
        self.comments_count = instances.count()
        self.save()
        return instances.all()

    @atomic
    @fetch_all(return_all=update_count_and_get_comments, paging_next_arg_name='after')
    def fetch_comments(self, limit=1000, filter='stream', summary=True, **kwargs):
        '''
        Retrieve and save all comments of post
        '''
        ids = []
        response = graph('%s/comments' % self.graph_id, limit=limit, filter=filter, summary=int(summary), **kwargs)
        if response:
            log.debug('response objects count=%s, limit=%s, after=%s' % (len(response.data), limit, kwargs.get('after')))
            for resource in response.data:
                instance = Comment.remote.get_or_create_from_resource(resource, {'post_id': self.pk})
                ids += [instance.pk]

        return Comment.objects.filter(pk__in=ids), response

    def save(self, *args, **kwargs):
        # set exactly Page or User contentTypes, not a child
        for field_name in ['author']:
            for allowed_model in [Page, User]:
                if isinstance(getattr(self, field_name), allowed_model):
                    setattr(self, '%s_content_type' % field_name, ContentType.objects.get_for_model(allowed_model))
                    break

        # check is generic fields has correct content_type
        if self.author_content_type:
            allowed_ct_ids = [ct.pk for ct in ContentType.objects.get_for_models(Page, User).values()]
            if self.author_content_type.pk not in allowed_ct_ids:
                raise ValueError("'author' field should be Page or User instance")

        return super(Post, self).save(*args, **kwargs)

    def get_url(self):
        try:
            owner_screenname = self.owners.all()[0].owner.username
        except:
            owner_screenname = ''
        return super(Post, self).get_url('%s/posts/%s' % (owner_screenname, self.graph_id.split('_')[1]))

class PostOwner(models.Model):
    '''
    Connection model for keeping multiple owners of single post
    '''
    class Meta:
        unique_together = ('post','owner_content_type','owner_id')

    post = models.ForeignKey(Post, related_name='owners')

    owner_content_type = models.ForeignKey(ContentType, null=True, related_name='facebook_page_posts')
    owner_id = models.PositiveIntegerField(null=True, db_index=True)
    owner = generic.GenericForeignKey('owner_content_type', 'owner_id')

    def save(self, *args, **kwargs):
        # set exactly right Page or User contentTypes, not a child
        for field_name in ['owner']:
            for allowed_model in [Page, User]:
                if isinstance(getattr(self, field_name), allowed_model):
                    setattr(self, '%s_content_type' % field_name, ContentType.objects.get_for_model(allowed_model))
                    break

        # check if generic fields has correct content_type
        if self.owner_content_type:
            allowed_ct_ids = [ct.pk for ct in ContentType.objects.get_for_models(Page, User, Application).values()]
            if self.owner_content_type.pk not in allowed_ct_ids:
                raise ValueError("'owner' field should be Page or User instance, not %s" % type(self.owner))

        return super(PostOwner, self).save(*args, **kwargs)

class Comment(FacebookGraphIDModel, FacebookLikableModel):
    class Meta:
        verbose_name = 'Facebook comment'
        verbose_name_plural = 'Facebook comments'

    like_users = models.ManyToManyField(User, related_name='like_comments')

    post = models.ForeignKey(Post, related_name='comments')
    author_json = fields.JSONField(null=True, help_text='Information about the user who posted the comment') # object containing the name and Facebook id of the user who posted the message

    author_content_type = models.ForeignKey(ContentType, null=True, related_name='facebook_comments')
    author_id = models.PositiveIntegerField(null=True, db_index=True)
    author = generic.GenericForeignKey('author_content_type', 'author_id')

    message = models.TextField(help_text='The message')
    created_time = models.DateTimeField(help_text='The time the comment was initially published', db_index=True)

    can_remove = models.BooleanField(default=False)
    user_likes = models.BooleanField(default=False)

    objects = models.Manager()
    remote = FacebookGraphManager()

    def save(self, *args, **kwargs):
        # set exactly right Page or User contentTypes, not a child
        for field_name in ['author']:
            for allowed_model in [Page, User]:
                if isinstance(getattr(self, field_name), allowed_model):
                    setattr(self, '%s_content_type' % field_name, ContentType.objects.get_for_model(allowed_model))
                    break

        # check is generic fields has correct content_type
        if self.author_content_type:
            allowed_ct_ids = [ct.pk for ct in ContentType.objects.get_for_models(Page, User).values()]
            if self.author_content_type.pk not in allowed_ct_ids:
                raise ValueError("'author' field should be Page or User instance")

        return super(Comment, self).save(*args, **kwargs)

    def parse(self, response):
        if 'from' in response:
            response['author_json'] = response.pop('from')
        if 'like_count' in response:
            response['likes_count'] = response.pop('like_count')

        # transform graph_id from {POST_ID}_{COMMENT_ID} -> {PAGE_ID}_{POST_ID}_{COMMENT_ID}
        if response['id'].count('_') == 1:
            response['id'] = re.sub(r'^\d+', self.post.graph_id, response['id'])

        super(Comment, self).parse(response)

        if self.author is None and self.author_json:
            self.author = get_or_create_from_small_resource(self.author_json)

    def get_url(self):
        try:
            owner_screenname = self.post.owners.all()[0].owner.username
        except:
            owner_screenname = ''
        post_id, comment_id = self.graph_id.split('_')[1:]
        return super(Comment, self).get_url('%s/posts/%s?comment_id=%s' % (owner_screenname, post_id, comment_id))