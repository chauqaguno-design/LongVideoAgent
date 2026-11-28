# Editing rules

- Use source seconds and keep every clip within the asset duration. Use half-open ranges: the start is included and the end is excluded.
- For a new rough cut, assemble the supported clips in the requested order. Use the asset ID or media revision ID, or omit it when there is one source. On an edit error, use the returned valid assets and current head revision to retry.
- Use `audio_fade_ms` (0–3000 ms, default 0) for fades at both ends of each clip with source audio. The render shortens fades on short clips.
- Keep source audio by default. Set captions when requested. Render only when the user asks for a preview or export. Previews use MP4. Exports support MP4, WebM and MKV.
- Report what the tools actually saved or rendered. Use the current revision and artifact IDs returned by the tools.
