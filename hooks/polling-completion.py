"""Automatic native completion collection, under the existing trim policy."""
import polling_runtime

EVENTS = ('UserPromptSubmit', 'PostToolBatch', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure')
FEATURE = 'trim'
BUDGET = 271


def handle(ctx):
    return polling_runtime.handle(ctx)
