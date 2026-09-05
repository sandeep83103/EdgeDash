"""Natural-language query subsystem.

tools.py holds the fixed registry of parameterised, read-only query functions.
A router model may only SELECT from this registry — it never composes SQL
(rule 40). Import the registry from edgedash.query.tools.
"""
