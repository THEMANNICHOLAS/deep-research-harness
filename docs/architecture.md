# Architecture

## Overview

To be documented as code is written. Direction so far: an orchestrator–worker
agent loop — a smart model (GLM 5.2, DeepSeek V4 Pro as config-swappable
fallback) plans and synthesizes, while a cheap fast model does parallel
triage/extraction under a rate/token budget scheduler. The worker role is
itself config-swappable (e.g. to an OpenCode-served model) if the initial
choice proves rate-limit-constrained in practice.

## Directory Structure

To be documented as code is written.

## Principles & Invariants

Full rule set with rationale lives here as it's established; the always-load
subset is in @docs/INDEX.md → CLAUDE.md `## Invariants`.

## Key Patterns

To be documented as code is written.

## Dependencies

To be documented as code is written.

## Failure Modes

To be documented as code is written.
