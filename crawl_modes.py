"""Page selection policies. Discovery, queueing and downloads share one coordinator."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Depth:
    internal: int = 0
    external: int = 0


class InternalMode:
    name = "internal"

    def saves_page(self, internal):
        return internal

    def next_depth(self, current, *, source_internal, target_internal, same_host,
                   internal_limit, external_limit):
        if not source_internal or not target_internal:
            return None
        depth = current.internal + 1
        return Depth(depth, 0) if not internal_limit or depth <= internal_limit else None


class FullMode(InternalMode):
    name = "full"

    def saves_page(self, internal):
        return True

    def next_depth(self, current, *, source_internal, target_internal, same_host,
                   internal_limit, external_limit):
        if target_internal:
            return super().next_depth(current, source_internal=source_internal,
                                      target_internal=True, same_host=same_host,
                                      internal_limit=internal_limit, external_limit=external_limit)
        if not source_internal and not same_host:
            return None
        depth = current.external + 1
        return Depth(current.internal, depth) if not external_limit or depth <= external_limit else None


class ExternalMode(FullMode):
    """Same traversal as Full; internal pages are discovery-only."""
    name = "external"

    def saves_page(self, internal):
        return not internal


def select_mode(follow_external, external_only):
    if external_only:
        return ExternalMode()
    return FullMode() if follow_external else InternalMode()
