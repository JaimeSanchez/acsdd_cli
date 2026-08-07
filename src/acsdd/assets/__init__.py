"""Files acsdd ships to be installed into a consumer repository.

Not importable content — these are data files, read via
``importlib.resources.files("acsdd.assets")`` so they resolve identically from
a source install and from the frozen PyInstaller binary. See
``acsdd.skills`` for the loader, and CLAUDE.md's "Packaged data files" note for
the two places every new file here has to be registered.
"""
