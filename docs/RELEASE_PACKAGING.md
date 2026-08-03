# Release packaging

End-user installations should use a GitHub Release asset rather than GitHub's
automatically generated source archives. Source archives may either contain Git LFS
pointers or charge each hydrated LFS download to the repository owner's monthly
bandwidth. GitHub Release assets are intended for binary distribution and do not have an
aggregate bandwidth limit.

From a checkout whose LFS files have been hydrated:

```powershell
git lfs pull
powershell -ExecutionPolicy Bypass -File tools\package-sonorus-release.ps1
```

The script packages the tracked `Phoenix` tree and the six root installer/readme files,
rejects any unexpanded LFS pointer, and writes both the installable ZIP and a SHA-256 file
under `dist/`. Development documentation, Git metadata, tests outside the shipped
`Phoenix` tree, and local/runtime data are excluded.

The native OmniVoice DLLs are intentionally not Git or LFS inputs. If a user selects the
OmniVoice Vulkan provider, the same installer downloads the pinned portable runtime
release and verifies its archive and per-file checksums before loading it.
