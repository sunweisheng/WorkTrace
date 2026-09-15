from __future__ import annotations

from copy import deepcopy


class FunctionRequestStub:
    def request_function(
        self,
        prompt,
        *,
        function_spec,
        allow_oversized_input=False,
    ):
        del prompt, allow_oversized_input
        return deepcopy(function_spec.typical_arguments)


class NullDelivery:
    def deliver_to_self(self, *, self_identity, markdown_path):
        return ("success", self_identity.open_id)
