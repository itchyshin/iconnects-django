"""Comment views: add comment, edit comment, mark read, delete.

Only a task's participants — its creator, assignees and reviewers — may post
on it, edit what they posted, and so tag people (``services.can_comment``).
Everyone in the project can still read the thread and mark comments read; an
author can always delete their own comment, and a Lead anyone's.
"""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.views import View

from apps.comments.forms import CommentForm
from apps.comments.models import Comment, CommentReadStatus
from apps.comments.services import (
    can_comment,
    mentionable_users,
    notify_mentioned_users,
    sync_mentions,
)
from apps.tasks.mixins import ProjectContextMixin, TaskMemberAccessMixin
from apps.tasks.models import Task

NOT_A_PARTICIPANT = "Only the task's creator, assignees and reviewers can comment."


def mention_candidates(project, user) -> list[dict]:
    """The people the @ list in a comment box offers ``user``: the project's
    contributors bar themselves, by name and email — the same two things the
    Contributors page already shows every member — so two people with one
    name can be told apart, and someone can be found by their address."""
    return [
        {"id": u.pk, "name": u.display_name, "email": u.email}
        for u in mentionable_users(project).exclude(pk=user.pk)
    ]


def comment_box_context(task, user) -> dict:
    """What the task surfaces need to draw the comment box, or not draw it."""
    allowed = can_comment(user, task)
    return {
        "can_comment": allowed,
        "mention_data": {
            "candidates": mention_candidates(task.project, user) if allowed else [],
            "selected": [],
        },
    }


class CommentCreateView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """HTMX endpoint: append a task-level comment."""

    task_url_kwarg = "task_pk"

    def post(self, request, *args, **kwargs):
        if self.project_member is None:
            return HttpResponseBadRequest("No project membership.")
        if not can_comment(request.user, self.task):
            return HttpResponse(NOT_A_PARTICIPANT, status=403)
        form = CommentForm(
            request.POST,
            mentionable=mentionable_users(self.project),
            author_user=request.user,
        )
        if not form.is_valid():
            return render(
                request,
                "comments/comment_form.html",
                {
                    "form": form,
                    "task": self.task,
                    "mention_data": {
                        "candidates": mention_candidates(self.project, request.user),
                        "selected": [],
                    },
                },
                status=400,
            )
        with transaction.atomic():
            comment: Comment = form.save(commit=False)
            comment.task = self.task
            comment.author = self.project_member
            comment.save()
            CommentReadStatus.objects.get_or_create(
                comment=comment,
                project_member=self.project_member,
                defaults={"read": True},
            )
            added = sync_mentions(comment, form.mentioned_users)
            notify_mentioned_users(comment, added, mentioned_by=request.user)
        return render(
            request,
            "comments/comment_item.html",
            {
                "comment": comment,
                "task": self.task,
                "project_member": self.project_member,
                "can_comment": True,
            },
        )


class CommentEditView(LoginRequiredMixin, View):
    """HTMX endpoint: inline-edit a comment.

    Its author only, and only while they can still comment on the task: an
    edit can tag people, so someone taken off the task cannot use one to do
    what they can no longer do with a new comment. They can still delete it.
    """

    def _load(self, request, pk):
        comment = get_object_or_404(
            Comment.objects.select_related("task__project", "task_log__task__project"),
            pk=pk,
        )
        task = self._get_task(comment)
        member = ProjectContextMixin._resolve_member(request.user, task.project)
        allowed = (
            member is not None
            and comment.author_id == member.id
            and can_comment(request.user, task)
        )
        return comment, task, member, allowed

    def _edit_form(self, request, form, comment, task, status=200):
        mentioned = comment.mentions.select_related("user")
        return render(
            request,
            "comments/comment_edit_form.html",
            {
                "form": form,
                "comment": comment,
                "mention_data": {
                    "candidates": mention_candidates(task.project, request.user),
                    "selected": [
                        {"id": m.user_id, "name": m.user.display_name, "email": m.user.email}
                        for m in mentioned
                    ],
                },
            },
            status=status,
        )

    def get(self, request, *args, **kwargs):
        comment, task, member, allowed = self._load(request, kwargs["pk"])
        if not allowed:
            return HttpResponse(status=403)
        if request.GET.get("cancel"):
            return render(
                request,
                "comments/comment_item.html",
                {
                    "comment": comment,
                    "task": task,
                    "project_member": member,
                    "can_comment": True,
                },
            )
        return self._edit_form(request, CommentForm(instance=comment), comment, task)

    def post(self, request, *args, **kwargs):
        comment, task, member, allowed = self._load(request, kwargs["pk"])
        if not allowed:
            return HttpResponse(status=403)
        form = CommentForm(
            request.POST,
            instance=comment,
            mentionable=mentionable_users(task.project),
            author_user=request.user,
        )
        if not form.is_valid():
            return self._edit_form(request, form, comment, task, status=400)
        with transaction.atomic():
            form.save()
            added = sync_mentions(comment, form.mentioned_users)
            if comment.task_id:
                notify_mentioned_users(comment, added, mentioned_by=request.user)
        return render(
            request,
            "comments/comment_item.html",
            {
                "comment": comment,
                "task": task,
                "project_member": member,
                "can_comment": True,
            },
        )

    @staticmethod
    def _get_task(comment: Comment) -> Task:
        if comment.task_id:
            return comment.task
        return comment.task_log.task


class CommentMarkReadView(LoginRequiredMixin, View):
    """HTMX endpoint: mark a single comment as read by the current member."""

    def post(self, request, *args, **kwargs):
        comment = get_object_or_404(
            Comment.objects.select_related("task__project", "task_log__task__project"),
            pk=kwargs["pk"],
        )
        if comment.task_id:
            project = comment.task.project
        else:
            project = comment.task_log.task.project
        member = ProjectContextMixin._resolve_member(request.user, project)
        if member is None:
            return HttpResponseBadRequest("No project membership.")
        CommentReadStatus.objects.update_or_create(
            comment=comment,
            project_member=member,
            defaults={"read": True},
        )
        return HttpResponse(status=204)


class CommentDeleteView(LoginRequiredMixin, View):
    """Delete a comment (author or project manager only)."""

    def post(self, request, *args, **kwargs):
        comment = get_object_or_404(
            Comment.objects.select_related("task__project", "task_log__task__project", "author"),
            pk=kwargs["pk"],
        )
        if comment.task_id:
            project = comment.task.project
        else:
            project = comment.task_log.task.project
        member = ProjectContextMixin._resolve_member(request.user, project)
        is_pm = ProjectContextMixin._is_project_manager(request.user, project)
        is_author = member is not None and comment.author_id == member.id
        if not (is_author or is_pm):
            return HttpResponse(status=403)
        comment.delete()
        return HttpResponse(status=204)
