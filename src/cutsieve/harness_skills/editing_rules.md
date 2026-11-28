# Editing rules

- Use source seconds and keep every clip within the asset duration. Use half-open ranges: the start is included and the end is excluded.
- For a new rough cut, assemble the supported clips in the requested order. For a revision, read the timeline and preserve clips and policies the user did not ask to change. Refresh the timeline after an optimistic revision conflict.
- Keep source audio by default. Set captions when requested. Render only when the user asks for a preview or export. Previews use MP4. Exports support MP4, WebM and MKV.
- Report what the tools actually saved or rendered. Use the current revision and artifact IDs returned by the tools.
