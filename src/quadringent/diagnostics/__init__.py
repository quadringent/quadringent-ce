"""Operator diagnostics shared by ``quadringent doctor``, the v2 API and the cockpit.

A diagnostic only observes: it never applies infrastructure, runs DDL or DML,
starts a pipeline, creates a Job or grants a privilege.
"""
