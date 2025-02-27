from django.conf import settings
from django.contrib.admin.utils import quote
from django.db import models, transaction
from django.forms import Media
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.text import capfirst
from django.utils.translation import gettext as _

from wagtail import hooks
from wagtail.admin import messages
from wagtail.admin.templatetags.wagtailadmin_tags import user_display_name
from wagtail.admin.ui.tables import TitleColumn
from wagtail.admin.utils import get_latest_str
from wagtail.locks import BasicLock, ScheduledForPublishLock
from wagtail.log_actions import log
from wagtail.log_actions import registry as log_registry
from wagtail.models import (
    DraftStateMixin,
    Locale,
    LockableMixin,
    RevisionMixin,
    TranslatableMixin,
)


class HookResponseMixin:
    """
    A mixin for class-based views to run hooks by `hook_name`.
    """

    def run_hook(self, hook_name, *args, **kwargs):
        """
        Run the named hook, passing args and kwargs to each function registered under that hook name.
        If any return an HttpResponse, stop processing and return that response
        """
        for fn in hooks.get_hooks(hook_name):
            result = fn(*args, **kwargs)
            if hasattr(result, "status_code"):
                return result
        return None


class BeforeAfterHookMixin(HookResponseMixin):
    """
    A mixin for class-based views to support hooks like `before_edit_page` and
    `after_edit_page`, which are triggered during execution of some operation and
    can return a response to halt that operation and/or change the view response.
    """

    def run_before_hook(self):
        """
        Define how to run the hooks before the operation is executed.
        The `self.run_hook(hook_name, *args, **kwargs)` from HookResponseMixin
        can be utilised to call the hooks.

        If this method returns a response, the operation will be aborted and the
        hook response will be returned as the view response, skipping the default
        response.
        """
        return None

    def run_after_hook(self):
        """
        Define how to run the hooks after the operation is executed.
        The `self.run_hook(hook_name, *args, **kwargs)` from HookResponseMixin
        can be utilised to call the hooks.

        If this method returns a response, it will be returned as the view
        response immediately after the operation finishes, skipping the default
        response.
        """
        return None

    def dispatch(self, *args, **kwargs):
        hooks_result = self.run_before_hook()
        if hooks_result is not None:
            return hooks_result

        return super().dispatch(*args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)

        hooks_result = self.run_after_hook()
        if hooks_result is not None:
            return hooks_result

        return response


class LocaleMixin:
    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.locale = self.get_locale()

    def get_locale(self):
        i18n_enabled = getattr(settings, "WAGTAIL_I18N_ENABLED", False)
        if hasattr(self, "model") and self.model:
            i18n_enabled = i18n_enabled and issubclass(self.model, TranslatableMixin)

        if not i18n_enabled:
            return None

        if hasattr(self, "object") and self.object:
            return self.object.locale

        selected_locale = self.request.GET.get("locale")
        if selected_locale:
            return get_object_or_404(Locale, language_code=selected_locale)
        return Locale.get_default()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.locale:
            return context

        context["locale"] = self.locale
        return context


class PanelMixin:
    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.panel = self.get_panel()

    def get_panel(self):
        return None

    def get_bound_panel(self, form):
        if not self.panel:
            return None
        return self.panel.get_bound_panel(
            request=self.request, instance=form.instance, form=form
        )

    def get_form_class(self):
        if not self.panel:
            return super().get_form_class()
        return self.panel.get_form_class()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        form = context.get("form")
        panel = self.get_bound_panel(form)

        media = context.get("media", Media())
        if form:
            media += form.media
        if panel:
            media += panel.media

        context.update(
            {
                "panel": panel,
                "media": media,
            }
        )

        return context


class IndexViewOptionalFeaturesMixin:
    """
    A mixin for generic IndexView to support optional features that are applied
    to the model as mixins (e.g. DraftStateMixin, RevisionMixin).
    """

    def _get_title_column(self, field_name, column_class=TitleColumn, **kwargs):
        accessor = kwargs.pop("accessor", None)

        if not accessor and field_name == "__str__":
            accessor = get_latest_str

        return super()._get_title_column(
            field_name, column_class, accessor=accessor, **kwargs
        )

    def _annotate_queryset_updated_at(self, queryset):
        if issubclass(queryset.model, RevisionMixin):
            # Use the latest revision's created_at
            queryset = queryset.select_related("latest_revision")
            queryset = queryset.annotate(
                _updated_at=models.F("latest_revision__created_at")
            )
            return queryset
        return super()._annotate_queryset_updated_at(queryset)


class CreateEditViewOptionalFeaturesMixin:
    """
    A mixin for generic CreateView/EditView to support optional features that
    are applied to the model as mixins (e.g. DraftStateMixin, RevisionMixin).
    """

    view_name = "create"
    lock_url_name = None
    unlock_url_name = None
    revisions_unschedule_url_name = None

    def setup(self, request, *args, **kwargs):
        # Need to set these here as they are used in get_object()
        self.request = request
        self.args = args
        self.kwargs = kwargs

        self.revision_enabled = self.model and issubclass(self.model, RevisionMixin)
        self.draftstate_enabled = self.model and issubclass(self.model, DraftStateMixin)
        self.locking_enabled = (
            self.model
            and issubclass(self.model, LockableMixin)
            and self.view_name != "create"
        )

        # Set the object before super().setup() as LocaleMixin.setup() needs it
        self.object = self.get_object()
        self.lock = self.get_lock()
        self.locked_for_user = self.lock and self.lock.for_user(request.user)
        super().setup(request, *args, **kwargs)

    def user_has_permission(self, permission):
        # Allow unlocking even if the user does not have the 'unlock' permission
        # if they are the user who locked the object
        if permission == "unlock" and self.object.locked_by_id == self.request.user.pk:
            return True
        return super().user_has_permission(permission)

    def get_available_actions(self):
        actions = [*super().get_available_actions()]

        if self.draftstate_enabled and (
            not self.permission_policy
            or self.permission_policy.user_has_permission(self.request.user, "publish")
        ):
            actions.append("publish")

        return actions

    def get_object(self, queryset=None):
        if self.view_name == "create":
            return None
        self.live_object = super().get_object(queryset)
        if self.draftstate_enabled:
            return self.live_object.get_latest_revision_as_object()
        return self.live_object

    def get_lock(self):
        if not self.locking_enabled:
            return None
        return self.object.get_lock()

    def get_lock_url(self):
        if not self.locking_enabled or not self.lock_url_name:
            return None
        return reverse(self.lock_url_name, args=[quote(self.object.pk)])

    def get_unlock_url(self):
        if not self.locking_enabled or not self.unlock_url_name:
            return None
        return reverse(self.unlock_url_name, args=[quote(self.object.pk)])

    def get_error_message(self):
        if self.locked_for_user:
            return capfirst(
                _("The %(model_name)s could not be saved as it is locked")
                % {"model_name": self.model._meta.verbose_name}
            )
        return super().get_error_message()

    def get_success_message(self, instance=None):
        object = instance or self.object

        message = _("%(model_name)s '%(object)s' updated.")
        if self.view_name == "create":
            message = _("%(model_name)s '%(object)s' created.")

        if self.draftstate_enabled and self.action == "publish":
            # Scheduled publishing
            if object.go_live_at and object.go_live_at > timezone.now():
                message = _(
                    "%(model_name)s '%(object)s' has been scheduled for publishing."
                )

                if self.view_name == "create":
                    message = _(
                        "%(model_name)s '%(object)s' created and scheduled for publishing."
                    )
                elif object.live:
                    message = _(
                        "%(model_name)s '%(object)s' is live and this version has been scheduled for publishing."
                    )

            # Immediate publishing
            else:
                message = _("%(model_name)s '%(object)s' updated and published.")
                if self.view_name == "create":
                    message = _("%(model_name)s '%(object)s' created and published.")

        return message % {
            "model_name": capfirst(self.model._meta.verbose_name),
            "object": object,
        }

    def get_success_url(self):
        if not self.draftstate_enabled or self.action == "publish":
            return super().get_success_url()

        # If DraftStateMixin is enabled and the action isn't publish,
        # remain on the edit view
        return self.get_edit_url()

    def save_instance(self):
        """
        Called after the form is successfully validated - saves the object to the db
        and returns the new object. Override this to implement custom save logic.
        """
        if self.draftstate_enabled:
            instance = self.form.save(commit=False)

            # If DraftStateMixin is applied, only save to the database in CreateView,
            # and make sure the live field is set to False.
            if self.view_name == "create":
                instance.live = False
                instance.save()
                self.form.save_m2m()
        else:
            instance = self.form.save()

        self.has_content_changes = self.view_name == "create" or self.form.has_changed()

        # Save revision if the model inherits from RevisionMixin
        self.new_revision = None
        if self.revision_enabled:
            self.new_revision = instance.save_revision(user=self.request.user)

        log(
            instance=instance,
            action="wagtail.create" if self.view_name == "create" else "wagtail.edit",
            revision=self.new_revision,
            content_changed=self.has_content_changes,
        )

        return instance

    def publish_action(self):
        hook_response = self.run_hook("before_publish", self.request, self.object)
        if hook_response is not None:
            return hook_response

        # Skip permission check as it's already done in get_available_actions
        self.new_revision.publish(user=self.request.user, skip_permission_checks=True)

        hook_response = self.run_hook("after_publish", self.request, self.object)
        if hook_response is not None:
            return hook_response

        return None

    def form_valid(self, form):
        self.form = form
        with transaction.atomic():
            self.object = self.save_instance()

        if self.action == "publish":
            response = self.publish_action()
            if response is not None:
                return response

        response = self.save_action()

        hook_response = self.run_after_hook()
        if hook_response is not None:
            return hook_response

        return response

    def get_live_last_updated_info(self):
        # Create view doesn't have last updated info
        if self.view_name == "create":
            return None

        # DraftStateMixin is applied but object is not live
        if self.draftstate_enabled and not self.object.live:
            return None

        revision = None
        # DraftStateMixin is applied and object is live
        if self.draftstate_enabled and self.object.live_revision:
            revision = self.object.live_revision
        # RevisionMixin is applied, so object is assumed to be live
        elif self.revision_enabled and self.object.latest_revision:
            revision = self.object.latest_revision

        # No mixin is applied or no revision exists, fall back to latest log entry
        if not revision:
            return log_registry.get_logs_for_instance(self.object).first()

        return {
            "timestamp": revision.created_at,
            "user_display_name": user_display_name(revision.user),
        }

    def get_lock_context(self):
        if not self.locking_enabled:
            return {}

        user_can_lock = not self.lock and self.user_has_permission("lock")
        user_can_unlock = (
            isinstance(self.lock, BasicLock)
        ) and self.user_has_permission("unlock")
        user_can_unschedule = (
            isinstance(self.lock, ScheduledForPublishLock)
        ) and self.user_has_permission("publish")

        context = {
            "lock": self.lock,
            "locked_for_user": self.locked_for_user,
            "lock_url": self.get_lock_url(),
            "unlock_url": self.get_unlock_url(),
            "user_can_lock": user_can_lock,
            "user_can_unlock": user_can_unlock,
        }

        if not self.lock:
            return context

        lock_message = self.lock.get_message(self.request.user)
        if lock_message:
            if user_can_unlock:
                lock_message = format_html(
                    '{} <span class="buttons"><button type="button" class="button button-small button-secondary" data-action-lock-unlock data-url="{}">{}</button></span>',
                    lock_message,
                    self.get_unlock_url(),
                    _("Unlock"),
                )

            if user_can_unschedule:
                lock_message = format_html(
                    '{} <span class="buttons"><button type="button" class="button button-small button-secondary" data-action-lock-unlock data-url="{}">{}</button></span>',
                    lock_message,
                    reverse(
                        self.revisions_unschedule_url_name,
                        args=[quote(self.object.pk), self.object.scheduled_revision.id],
                    ),
                    _("Cancel scheduled publish"),
                )

            if self.locked_for_user:
                messages.error(self.request, lock_message, extra_tags="lock")
            else:
                messages.warning(self.request, lock_message, extra_tags="lock")

        return context

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_lock_context())
        context["revision_enabled"] = self.revision_enabled
        context["draftstate_enabled"] = self.draftstate_enabled
        context["live_last_updated_info"] = self.get_live_last_updated_info()
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        # Make sure object is not locked
        if not self.locked_for_user and form.is_valid():
            return self.form_valid(form)
        else:
            return self.form_invalid(form)


class RevisionsRevertMixin:
    revision_id_kwarg = "revision_id"
    revisions_revert_url_name = None

    def setup(self, request, *args, **kwargs):
        self.revision_id = kwargs.get(self.revision_id_kwarg)
        super().setup(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        self._add_warning_message()
        return super().get(request, *args, **kwargs)

    def get_revisions_revert_url(self):
        return reverse(
            self.revisions_revert_url_name,
            args=[quote(self.object.pk), self.revision_id],
        )

    def get_warning_message(self):
        user_avatar = render_to_string(
            "wagtailadmin/shared/user_avatar.html", {"user": self.revision.user}
        )
        message_string = _(
            "You are viewing a previous version of this %(model_name)s from <b>%(created_at)s</b> by %(user)s"
        )
        message_data = {
            "model_name": capfirst(self.model._meta.verbose_name),
            "created_at": self.revision.created_at.strftime("%d %b %Y %H:%M"),
            "user": user_avatar,
        }
        message = mark_safe(message_string % message_data)
        return message

    def _add_warning_message(self):
        messages.warning(self.request, self.get_warning_message())

    def get_object(self, queryset=None):
        object = super().get_object(queryset)
        self.revision = get_object_or_404(object.revisions, id=self.revision_id)
        return self.revision.as_object()

    def save_instance(self):
        commit = not issubclass(self.model, DraftStateMixin)
        instance = self.form.save(commit=commit)

        self.has_content_changes = self.form.has_changed()

        self.new_revision = instance.save_revision(
            user=self.request.user,
            log_action=True,
            previous_revision=self.revision,
        )

        return instance

    def get_success_message(self):
        message = _(
            "%(model_name)s '%(object)s' has been replaced with version from %(timestamp)s."
        )
        if self.draftstate_enabled and self.action == "publish":
            message = _(
                "Version from %(timestamp)s of %(model_name)s '%(object)s' has been published."
            )

            if self.object.go_live_at and self.object.go_live_at > timezone.now():
                message = _(
                    "Version from %(timestamp)s of %(model_name)s '%(object)s' has been scheduled for publishing."
                )

        return message % {
            "model_name": capfirst(self.model._meta.verbose_name),
            "object": self.object,
            "timestamp": self.revision.created_at.strftime("%d %b %Y %H:%M"),
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["revision"] = self.revision
        context["action_url"] = self.get_revisions_revert_url()
        return context
