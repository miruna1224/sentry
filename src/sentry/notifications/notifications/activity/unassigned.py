from __future__ import annotations

from typing import Any, Mapping

from .base import GroupActivityNotification


class UnassignedActivityNotification(GroupActivityNotification):
    metrics_key = "unassigned_activity"
    title = "Unassigned"

    def get_description(self) -> tuple[str, Mapping[str, Any], Mapping[str, Any]]:
        return "{author} unassigned {an issue}", {}, {}

    def get_notification_title(self) -> str:
        user = self.activity.user
        if user:
            author = user.name or user.email
        else:
            author = "Sentry"
        return f"Issue unassigned by {author}"
