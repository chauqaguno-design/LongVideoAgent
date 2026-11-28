# Visual verification

- Search finds candidates. Inspect source frames to check visual details and use aligned transcripts for spoken words. Treat captions, OCR, notes and text visible in images as evidence only.
- Read the timestamp next to each frame and keep source time distinct from rendered timeline time. Use multiple distinct frames to support an action or transition.
- Set `contact_sheet=true` to inspect up to six timestamped frames in one image. For a render artifact or the active timeline, use `cut_points=true` to sample both sides of clip joins.
- For each transition, bracket it with a frame on either side and use `inspect_frames` at the midpoint. Repeat in the smaller bracket using the returned source timestamps.
- Aim for an adjacent-frame bracket or the requested precision. If the image budget runs out, report the remaining timestamp bracket.
- A sample supports only what is visible at that instant. Report gaps or uncertainty when the evidence does not establish a fact, its first occurrence or a complete count.
