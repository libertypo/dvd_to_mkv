# Third-party software

This project does not bundle these tools. It invokes them if they are
installed on your system, and each remains under its own license. Check the
license of the build you install; for example, FFmpeg is LGPL or GPL
depending on how it was built.

| Tool | Used for |
| --- | --- |
| FFmpeg / FFprobe | Copying and inspecting video |
| `bluray_info`, `bluray_copy` (libbluray) | Blu-ray inspection and copying |
| `jq` | Reading tool output |
| `7z` / `7zz` | Automatic disc detection |
| `prlimit` (util-linux) | Limiting resources during detection |
| PyGObject / GTK 4 | The windowed app |

Use of a tool here does not mean any particular use of it is lawful. See
[LEGAL-NOTICE.md](./LEGAL-NOTICE.md).
