from __future__ import annotations

from copy import deepcopy
import json


def personal_render_payload(prompt):
    """Build a test response from test input, independent of prompt examples."""
    group = json.loads(prompt)["locked_group"]
    members = group["members"]
    primary = members[0]
    facts = [
        {
            "field": "topic", "text": primary["topic"],
            "evidence_message_ids": primary["source_message_ids"][:1],
            "actor": "context",
        },
        *[
            {
                "field": "content", "text": member["content"],
                "evidence_message_ids": member["source_message_ids"],
                "actor": "context",
            }
            for member in members
        ],
        {
            "field": "object_hint", "text": primary["object_hint"],
            "evidence_message_ids": primary["source_message_ids"][:1],
            "actor": "context",
        },
        *[
            {
                "field": field, "text": primary[field],
                "evidence_message_ids": primary["source_message_ids"],
                "actor": "context",
            }
            for field in (
                "action_label", "retention_reason", "retention_detail",
            )
        ],
    ]
    return {"groups": [{
        "group_id": group["group_id"],
        "covered_draft_ids": group["draft_ids"],
        "supported": True,
        "self_actions": [],
        "removed_claims": [],
        "fact_items": facts,
    }]}


class FunctionRequestStub:
    def request_function(
        self,
        prompt,
        *,
        function_spec,
        allow_oversized_input=False,
    ):
        del allow_oversized_input
        if function_spec.request_kind == "personal_group_render":
            return personal_render_payload(prompt)
        return deepcopy(function_spec.typical_arguments)


class NullDelivery:
    def deliver_to_self(self, *, self_identity, markdown_path):
        return ("success", self_identity.open_id)
