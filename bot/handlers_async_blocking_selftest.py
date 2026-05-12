"""Selftests for keeping blocking network/AI helpers off the bot event loop."""

from __future__ import annotations

import ast
from pathlib import Path

HANDLERS_PATH = Path(__file__).with_name("handlers.py")

BLOCKING_HELPERS = {
    "collect_topics",
    "collect_topics_with_diagnostics",
    "fetch_page_content",
    "generate_post_draft",
    "generate_post_draft_from_page",
    "polish_post_draft",
    "translate_topic_title_to_ru",
    "enrich_topic_metadata_ru",
}

EXPECTED_WRAPPERS = {
    "_run_collect_topics": "collect_topics",
    "_run_collect_topics_with_diagnostics": "collect_topics_with_diagnostics",
    "_run_fetch_page_content": "fetch_page_content",
    "_run_generate_post_draft": "generate_post_draft",
    "_run_generate_post_draft_from_page": "generate_post_draft_from_page",
    "_run_polish_post_draft": "polish_post_draft",
    "_run_translate_topic_title_to_ru": "translate_topic_title_to_ru",
    "_run_enrich_topic_metadata_ru": "enrich_topic_metadata_ru",
}


class _DirectBlockingCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.async_stack: list[str] = []
        self.violations: list[tuple[int, str, str]] = []

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.async_stack.append(node.name)
        self.generic_visit(node)
        self.async_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # A nested sync helper is not itself running as async handler code.
        if not self.async_stack:
            self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.async_stack and self.async_stack[-1] not in EXPECTED_WRAPPERS:
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKING_HELPERS:
                self.violations.append((node.lineno, self.async_stack[-1], node.func.id))
        self.generic_visit(node)


def _has_to_thread_call(wrapper: ast.AsyncFunctionDef, helper_name: str) -> bool:
    for node in ast.walk(wrapper):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "to_thread"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        ):
            continue
        if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == helper_name:
            return True
    return False


def run() -> None:
    source = HANDLERS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wrappers = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}

    missing_wrappers = []
    for wrapper_name, helper_name in EXPECTED_WRAPPERS.items():
        wrapper = wrappers.get(wrapper_name)
        if wrapper is None or not _has_to_thread_call(wrapper, helper_name):
            missing_wrappers.append(f"{wrapper_name} -> asyncio.to_thread({helper_name}, ...)")
    assert not missing_wrappers, "Missing to_thread wrappers: " + ", ".join(missing_wrappers)

    visitor = _DirectBlockingCallVisitor()
    visitor.visit(tree)
    assert not visitor.violations, "Direct blocking calls in async functions: " + ", ".join(
        f"line {line} in {func} calls {helper}" for line, func, helper in visitor.violations
    )

    print("OK")


if __name__ == "__main__":
    run()
