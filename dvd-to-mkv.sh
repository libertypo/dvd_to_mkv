#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: dvd-to-mkv.sh --list-streams INPUT [TITLE_OR_PLAYLIST]
       dvd-to-mkv.sh --list-streams-tsv INPUT [TITLE_OR_PLAYLIST]
       dvd-to-mkv.sh [--stream INDEX]... INPUT OUTPUT [TITLE_OR_PLAYLIST]
       dvd-to-mkv.sh --bluray [--device DEVICE] [--keydb FILE] [--minlength MINUTES] --list-playlists
       dvd-to-mkv.sh --bluray [--device DEVICE] [--keydb FILE] --playlist ID --list-streams
       dvd-to-mkv.sh --bluray [--device DEVICE] [--keydb FILE] [--playlist ID]... [--stream-id ID:PID]... [OUTPUT]

Remux a DVD title or Blu-ray playlist to Matroska without re-encoding.

Without --bluray, INPUT may be a DVD device, ISO image, or VIDEO_TS directory.
TITLE_OR_PLAYLIST defaults to 1.

Use --list-streams to print each stream's index, type, codec, language, and title.
Use --list-streams-tsv for the same DVD metadata as tab-separated values.
Repeat --stream INDEX to copy only selected stream indexes. By default all streams copy.
For Blu-ray, --playlist ID --list-streams lists stream PIDs for one playlist.
Repeat --stream-id ID:PID to keep only those PIDs for that playlist (for example 0:0x1100).
Playlists without --stream-id selections copy all streams.
Use --list-playlists to show Blu-ray playlists at least MINUTES long (default: 2).
Repeat --playlist ID to copy selected Blu-ray playlists. OUTPUT defaults to the current directory;
with one playlist it may be an MKV filename, otherwise it must be an existing directory (a
missing one is not created automatically).
Use --keydb to set the AACS KEYDB.cfg; if omitted, one is auto-detected under ~/.config/aacs/,
otherwise bluray_copy/bluray_info fall back to their own default AACS database.
Use --retries N to retry a failed or invalid disc copy up to N extra times (0-9999, default: 0).
Use --output-name NAME with Blu-ray output directories to name a single playlist NAME.mkv,
or multiple playlists NAME-playlist-ID.mkv. Explicit MKV output paths take precedence.
Playlist listings include a "# disc-name" metadata line when a disc name or UDF title exists.
3D discs include compatibility warnings; stream inspection also requires FFprobe with Blu-ray support.
Use --output-format auto|mkv|m2ts for Blu-ray (default: auto).
Auto preserves all streams as M2TS when MVC payload is verified in a playlist sample.
M2TS output does not support --stream-id filtering and requires a .m2ts filename or directory.
Blu-ray MKV copies embed the original playlist chapters. Original M2TS copies export
chapter markers to OUTPUT.m2ts.chapters.txt (OGM chapter format), without altering the video.

Still confused? Don't worry, so is everyone the first time. Run it once, read the error, repeat.
EOF
}

media_type=dvd
list_streams=false
list_streams_tsv=false
selected_streams=()
selected_stream_ids=()
declare -A seen_stream_ids=()
declare -A seen_streams=()
list_playlists=false
selected_playlists=()
declare -A seen_playlists=()
device=
output_name=
keydb=
output_format=auto
output_format_set=false
declare -A playlist_formats=()
warning_3d='3D content reported by disc. Auto output uses original M2TS only when playlist MVC payload is verified; original M2TS preserves all streams.'
minlength=2
minlength_set=false
retries=0
temporary_output=
temporary_copy=
temporary_directory=
workspace_parent_fd=
workspace_parent=
publication_pending=false
pending_output=
pending_chapter_sidecar=
pending_chapter_source=
IFS= read -r -d '' module_directory < <(dirname -z -- "$(readlink -f -- "$0")")

cleanup() {
    if [[ $publication_pending == true ]]; then
        # Remove only links created by this attempt, never a competing destination.
        if [[ ! -L $pending_output && $pending_output -ef $temporary_output ]]; then
            rm -f -- "$pending_output"
        fi
        if [[ -n $pending_chapter_sidecar && ! -L $pending_chapter_sidecar \
            && $pending_chapter_sidecar -ef $pending_chapter_source ]]; then
            rm -f -- "$pending_chapter_sidecar"
        fi
    fi
    if [[ -n $temporary_directory ]]; then
        rm -rf "$temporary_directory"
    fi
}

make_temporary_workspace() {
    local output_directory=$1
    local owner mode

    # Pin the directory before checking it: ancestor renames/symlink swaps must
    # not redirect subsequent writes or rollback to another directory.
    exec {workspace_parent_fd}< "$output_directory"
    workspace_parent=/proc/$$/fd/$workspace_parent_fd
    if [[ ! -d $workspace_parent ]] \
        || ! read -r owner mode < <(stat -Lc '%u %a' -- "$workspace_parent") \
        || [[ $owner != "$EUID" && $owner != 0 ]] \
        || (( (8#$mode & 0022) != 0 && (8#$mode & 01000) == 0 )); then
        echo "Unsafe output directory: must be owned by you or root and not group/other-writable unless sticky: $output_directory" >&2
        exit 1
    fi

    # In a trusted parent, other users cannot replace our private workspace.
    temporary_directory=$(mktemp -d "$workspace_parent/.dvd-to-mkv.XXXXXX")
    temporary_output=$temporary_directory/output.mkv
    temporary_copy=$temporary_directory/output.m2ts
}

find_keydb() {
    local candidate

    for candidate in "$HOME"/.config/aacs/KEYDB.cfg "$HOME"/.config/aacs/keydb.cfg; do
        if [[ -f $candidate ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

probe_mvc() {
    if ! command -v python3 >/dev/null || ! command -v bluray_copy >/dev/null; then
        echo 'MVC payload inspection requires python3 and bluray_copy.' >&2
        return 1
    fi
    (
        set +e
        bluray_copy "$device" "${keydb_args[@]}" --playlist "$1" --output - |
            PYTHONPATH="$module_directory:${PYTHONPATH:-}" python3 -m dvdvideo.mvc_probe
        statuses=("${PIPESTATUS[@]}")
        if (( statuses[1] != 0 || (statuses[0] != 0 && statuses[0] != 141) )); then
            echo "MVC inspection failed for playlist $1 (reader ${statuses[0]}, parser ${statuses[1]})." >&2
            exit 1
        fi
    )
}

playlist_output_path() {
    local playlist=$1
    local extension=${playlist_formats[$playlist]:-mkv}

    if [[ -n $output_file ]]; then
        printf '%s\0' "$output_file"
    elif [[ -n $output_name && ${#selected_playlists[@]} -eq 1 ]]; then
        printf '%s/%s.%s\0' "$output_directory" "$output_name" "$extension"
    elif [[ -n $output_name ]]; then
        printf '%s/%s-playlist-%s.%s\0' "$output_directory" "$output_name" "$playlist" "$extension"
    else
        printf '%s/playlist-%s.%s\0' "$output_directory" "$playlist" "$extension"
    fi
}

publish_output() {
    local output_file=$workspace_parent/${1##*/}
    local chapter_file=${2:-}

    if [[ ! -f $temporary_output || -L $temporary_output ]] \
        || { [[ -n $chapter_file ]] && [[ ! -f $chapter_file || -L $chapter_file ]]; }; then
        echo "Refusing to publish a non-regular file or symbolic link: $1" >&2
        exit 1
    fi
    pending_output=$output_file
    pending_chapter_source=$chapter_file
    pending_chapter_sidecar=
    if [[ -n $chapter_file ]]; then
        pending_chapter_sidecar=$output_file.chapters.txt
    fi
    publication_pending=true
    if [[ -n $chapter_file ]]; then
        if ! ln -T -- "$chapter_file" "$output_file.chapters.txt"; then
            echo "Failed to publish chapters (existing files are not overwritten): $1.chapters.txt" >&2
            exit 1
        fi
    fi

    if ! ln -T -- "$temporary_output" "$output_file"; then
        if [[ -e $output_file || -L $output_file ]]; then
            echo "Refusing to overwrite existing file: $1" >&2
        else
            echo "Failed to publish output: $1" >&2
        fi
        exit 1
    fi
    publication_pending=false
    rm -rf "$temporary_directory"
    temporary_directory=
    temporary_output=
    temporary_copy=
    exec {workspace_parent_fd}<&-
    workspace_parent_fd=
    workspace_parent=
}

validate_output_filename() {
    local path=$1 directory filename limit
    local LC_ALL=C

    filename=${path##*/}
    directory=${path%/*}
    if [[ $directory == "$path" ]]; then
        directory=.
    elif [[ -z $directory ]]; then
        directory=/
    fi
    if ! limit=$(getconf NAME_MAX "$directory"); then
        echo "Cannot determine output filename limit: $directory" >&2
        exit 1
    fi
    if [[ $limit =~ ^[0-9]+$ ]]; then
        if (( limit > 0 && ${#filename} > limit )); then
            echo "Output filename exceeds the filesystem's $limit-byte limit: $path" >&2
            exit 2
        fi
    elif [[ $limit != undefined && $limit != -1 ]]; then
        echo "Invalid output filename limit for $directory: $limit" >&2
        exit 1
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            usage
            exit 0
            ;;
        --bluray)
            media_type=bluray
            shift
            ;;
        --list-streams)
            list_streams=true
            shift
            ;;
        --list-streams-tsv)
            list_streams=true
            list_streams_tsv=true
            shift
            ;;
        --list-playlists)
            list_playlists=true
            shift
            ;;
        --output-format)
            if [[ $# -lt 2 || ! $2 =~ ^(auto|mkv|m2ts)$ ]]; then
                echo '--output-format requires auto, mkv, or m2ts.' >&2
                exit 2
            fi
            output_format=$2
            output_format_set=true
            shift 2
            ;;
        --stream)
            if [[ $# -lt 2 || ! $2 =~ ^[0-9]{1,4}$ ]]; then
                echo '--stream requires a stream index from 0 to 9999.' >&2
                exit 2
            fi
            # Force base 10: a leading zero would otherwise be octal, and 1 and 01 must match.
            stream_index=$((10#$2))
            if [[ -v "seen_streams[$stream_index]" ]]; then
                echo "Duplicate stream index: $stream_index" >&2
                exit 2
            fi
            seen_streams[$stream_index]=1
            selected_streams+=("$stream_index")
            shift 2
            ;;
        --stream-id)
            # MPEG-TS PIDs are 13 bits: 0x0000-0x1fff.
            if [[ $# -lt 2 || ! $2 =~ ^[0-9]{1,5}:0x0{0,4}[0-9a-fA-F]{1,4}$ ]] \
                || (( 16#${2#*:0x} > 0x1fff )); then
                echo '--stream-id requires PLAYLIST:PID with a PID from 0x0000 to 0x1fff, for example 0:0x1100.' >&2
                exit 2
            fi
            selection=$((10#${2%%:*})):$(printf '0x%04x' "$((16#${2#*:0x}))")
            if [[ -v "seen_stream_ids[$selection]" ]]; then
                echo "Duplicate stream selection: $selection" >&2
                exit 2
            fi
            seen_stream_ids[$selection]=1
            selected_stream_ids+=("$selection")
            shift 2
            ;;
        --device|--keydb|--minlength|--playlist|--retries|--output-name)
            if [[ $# -lt 2 ]]; then
                echo "$1 requires a value." >&2
                exit 2
            fi
            case $1 in
                --device) device=$2 ;;
                --keydb) keydb=$2 ;;
                --output-name)
                    if [[ -z $2 || $2 == . || $2 == .. || $2 == */* || $2 == *\\* || $2 =~ [[:cntrl:]] ]]; then
                        echo '--output-name requires a non-empty filename stem without path separators or control characters.' >&2
                        exit 2
                    fi
                    output_name=$2
                    ;;
                --minlength)
                    if ! [[ $2 =~ ^[0-9]+$ ]]; then
                        echo '--minlength must be a non-negative number of minutes.' >&2
                        exit 2
                    fi
                    minlength=$2
                    minlength_set=true
                    ;;
                --playlist)
                    if ! [[ $2 =~ ^[0-9]{1,5}$ ]]; then
                        echo '--playlist requires a playlist ID from 0 to 99999.' >&2
                        exit 2
                    fi
                    selected_playlists+=("$((10#$2))")
                    ;;
                --retries)
                    if ! [[ $2 =~ ^[0-9]{1,4}$ ]]; then
                        echo '--retries must be a number of attempts from 0 to 9999.' >&2
                        exit 2
                    fi
                    # Base 10: "08" would otherwise abort the retry check and publish a failed copy.
                    retries=$((10#$2))
                    ;;
            esac
            shift 2
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            break
            ;;
    esac
done

if [[ $list_streams == true && (${#selected_streams[@]} -ne 0 || ${#selected_stream_ids[@]} -ne 0 || $list_playlists == true) ]]; then
    echo '--list-streams cannot be combined with stream selections or --list-playlists.' >&2
    exit 2
fi
if [[ $list_playlists == true && ${#selected_playlists[@]} -ne 0 ]]; then
    echo '--list-playlists cannot be combined with --playlist.' >&2
    exit 2
fi

if [[ $media_type == bluray && $list_playlists == false && $minlength_set == true ]]; then
    echo '--minlength can only be used with --list-playlists.' >&2
    exit 2
fi

if [[ $media_type != bluray && ($list_playlists == true || ${#selected_playlists[@]} -ne 0 || ${#selected_stream_ids[@]} -ne 0 || -n $device || -n $keydb || -n $output_name || $minlength_set == true || $output_format_set == true) ]]; then
    echo 'Blu-ray options require --bluray.' >&2
    exit 2
fi

if [[ $media_type == bluray && $list_streams_tsv == true ]]; then
    echo '--list-streams-tsv is only supported for DVD input.' >&2
    exit 2
fi

if [[ $media_type == bluray ]]; then
    if [[ ${#selected_streams[@]} -ne 0 ]]; then
        echo '--stream is only supported for DVD input.' >&2
        exit 2
    fi
    if [[ -z $device ]]; then
        device=/dev/sr0
    fi
    if [[ $device == -* ]]; then
        echo "Blu-ray input must not begin with a dash: $device" >&2
        exit 2
    fi
    if ! [[ -b $device || -f $device || -d $device ]]; then
        echo "Blu-ray input must be an existing device, image file, or BDMV directory: $device" >&2
        exit 2
    fi
    if [[ -d $device ]]; then
        IFS= read -r -d '' device_folder < <(basename -z -- "$device")
        if [[ ${device_folder^^} == BDMV ]]; then
            IFS= read -r -d '' device < <(dirname -z -- "$device")
        fi
    fi
    if [[ -z $keydb ]]; then
        keydb=$(find_keydb || true)
    fi
    if [[ $keydb == -* ]]; then
        keydb=./$keydb
    fi
    if [[ -n $keydb && ! -f $keydb ]]; then
        echo "KEYDB.cfg file does not exist: $keydb" >&2
        exit 2
    fi
    if [[ -n $keydb ]]; then
        keydb_permissions=$(stat -c '%a' "$keydb")
        if (( (8#$keydb_permissions & 0077) != 0 )); then
            echo "KEYDB.cfg must not be readable by group or others: $keydb" >&2
            exit 1
        fi
    fi
    keydb_args=()
    if [[ -n $keydb ]]; then
        keydb_args=(-k "$keydb")
    fi
    if [[ $list_streams == true ]]; then
        if [[ ${#selected_playlists[@]} -ne 1 || $# -ne 0 ]]; then
            echo 'Listing Blu-ray streams requires exactly one --playlist ID and no output path.' >&2
            exit 2
        fi
        if ! command -v bluray_info >/dev/null || ! command -v jq >/dev/null; then
            echo 'Listing Blu-ray streams requires bluray_info and jq.' >&2
            exit 1
        fi
        stream_metadata=$(bluray_info "$device" "${keydb_args[@]}" \
            --playlist "${selected_playlists[0]}" --all --json)
        probe_metadata='{"streams":[]}'
        is_3d=$(jq -r '.bluray["3D content"] == true' <<<"$stream_metadata")
        if [[ $is_3d == true ]]; then
            echo "$warning_3d" >&2
            if ! command -v ffprobe >/dev/null; then
                echo 'Inspecting 3D video streams requires ffprobe with Blu-ray protocol support.' >&2
                exit 1
            fi
            probe_metadata=$(ffprobe -v error -playlist "${selected_playlists[0]}" \
                -analyzeduration 10000000 -probesize 10000000 -select_streams v \
                -show_entries stream=id,codec_name,width,height -of json "bluray:$device")
            jq -e '.streams | type == "array" and length > 0' <<<"$probe_metadata" >/dev/null || {
                echo 'FFprobe did not return video streams for the 3D playlist.' >&2
                exit 1
            }
            mvc_streams=$(probe_mvc "${selected_playlists[0]}")
        else
            mvc_streams=
        fi
        stream_listing=$(jq -r --argjson probe "$probe_metadata" --argjson three_d "$is_3d" '
            if (.titles | type) != "array" or (.titles | length) != 1 then
                error("bluray_info must return exactly one title")
            else
                .titles[0] |
                (.video // [] | map(.stream | ascii_downcase)) as $known_video |
                (["video", "audio", "subtitles"][] as $kind |
                    .[$kind][]? |
                    if (.stream | type) != "string" or
                        (.stream | test("^0x[0-9a-fA-F]+$") | not) then
                        error("bluray_info stream does not contain a valid PID")
                    else
                        . as $record |
                        ([$probe.streams[] | select((.id | ascii_downcase) == ($record.stream | ascii_downcase))][0]) as $video |
                        [.stream, $kind, (.codec // .["codec name"] // ""),
                         (.language // ""), (.format // "")] +
                        (if $three_d then
                            [if $kind != "video" then ""
                             elif $video == null then "Video role/dimensions unverified"
                             elif ($video.width // 0) == 0 or ($video.height // 0) == 0 then
                                 "Dimensions unknown - MKV muxing unsupported"
                             else "\($video.width)x\($video.height) - video listed by disc metadata; not proof of full 3D"
                             end]
                         else [] end) | @tsv
                    end),
                (if $three_d then
                    $probe.streams[] | .id as $pid |
                    if (.id | type) != "string" or (.id | test("^0x[0-9a-fA-F]+$") | not) then
                        error("FFprobe video stream does not contain a valid PID")
                    elif ($known_video | index($pid | ascii_downcase)) != null then empty
                    else
                        [.id, "video", (.codec_name // ""), "", "",
                         (if (.width // 0) == 0 or (.height // 0) == 0 then
                             "Additional video - possible MVC-dependent view; dimensions unknown; MKV muxing unsupported"
                          else "Additional video - role unverified; \(.width)x\(.height)"
                          end)] | @tsv
                    end
                 else empty end)
            end' <<<"$stream_metadata")
        if [[ $is_3d == true ]]; then
            printf '# warning\t%s\n' "$warning_3d"
            if [[ -n $mvc_streams ]]; then
                while IFS= read -r mvc_pid; do
                    printf '# mvc\t%s\n' "$mvc_pid"
                done <<<"$mvc_streams"
            fi
            printf 'PID\tTYPE\tCODEC\tLANG\tFORMAT\tDETAILS\n'
        else
            printf 'PID\tTYPE\tCODEC\tLANG\tFORMAT\n'
        fi
        if [[ -n $stream_listing ]]; then
            printf '%s\n' "$stream_listing"
        else
            echo 'No streams found in the selected Blu-ray playlist.' >&2
            exit 1
        fi
        exit 0
    fi
    if [[ $list_playlists == true ]]; then
        if [[ ${#selected_stream_ids[@]} -ne 0 ]]; then
            echo '--list-playlists cannot be combined with --stream-id.' >&2
            exit 2
        fi
        if [[ $# -ne 0 ]]; then
            usage >&2
            exit 2
        fi
        if ! command -v bluray_info >/dev/null || ! command -v jq >/dev/null; then
            echo 'Listing Blu-ray playlists requires bluray_info and jq.' >&2
            exit 1
        fi
        playlist_listing=$(bluray_info "$device" "${keydb_args[@]}" --minutes "$minlength" --json | jq -r --arg warning "$warning_3d" \
            'if (.titles | type) != "array" then
                error("bluray_info JSON does not contain a titles array")
            else
                ([.bluray["disc name"], .bluray["udf title"]] |
                    map(select(type == "string") | gsub("^\\s+|\\s+$"; "") | select(length > 0)) |
                    .[0] // "") as $name |
                (if $name != "" then "# disc-name\t" + ($name | gsub("[\u0000-\u001f\u007f]"; " ")) else empty end),
                (if .bluray["3D content"] == true then "# warning\t" + $warning else empty end),
                (["main playlist", "longest playlist"][] as $kind |
                    .bluray[$kind] |
                    if type == "number" or type == "string" then
                        "# " + ($kind | gsub(" "; "-")) + "\t" + tostring
                    else empty end),
                (.titles[] |
                if (.playlist | type) != "number" and (.playlist | type) != "string" then
                    error("bluray_info title does not contain a playlist ID")
                else
                    [
                        (.playlist | tostring),
                        (.length // .duration // "unknown"),
                        (if (.chapter_count | type) == "number" or (.chapter_count | type) == "string" then
                            .chapter_count
                        elif (.chapters | type) == "array" then
                            (.chapters | length)
                        else
                            "unknown"
                        end)
                    ] | @tsv
                end
                )
            end')
        playlist_listing=$(LC_ALL=C tr -d '\000-\010\013\014\016-\037' <<<"$playlist_listing")
        printf 'playlist\treported_duration\tchapters\n'
        if [[ -n $playlist_listing ]]; then
            printf '%s\n' "$playlist_listing"
        else
            printf 'No playlists found.\n' >&2
        fi
        exit 0
    fi
    if [[ ${#selected_playlists[@]} -eq 0 ]]; then
        echo 'Select at least one Blu-ray playlist with --playlist ID, or use --list-playlists.' >&2
        exit 2
    fi
    if [[ $# -gt 1 ]]; then
        usage >&2
        exit 2
    fi
    output_path=${1:-.}
    if [[ $output_path == -* ]]; then
        output_path=./$output_path
    fi
    if [[ -d $output_path ]]; then
        output_directory=$output_path
        output_file=
    elif [[ ${#selected_playlists[@]} -eq 1 ]]; then
        IFS= read -r -d '' output_directory < <(dirname -z -- "$output_path")
        output_file=$output_path
        if ! [[ -d $output_directory ]]; then
            echo "Output directory does not exist: $output_directory" >&2
            exit 2
        fi
    else
        echo 'Multiple Blu-ray playlists require an existing output directory.' >&2
        exit 2
    fi
    for playlist in "${selected_playlists[@]}"; do
        format=$output_format
        if [[ $format == auto ]]; then
            if ! command -v bluray_info >/dev/null || ! command -v jq >/dev/null; then
                echo 'Automatic output format requires bluray_info and jq.' >&2
                exit 1
            fi
            disc_metadata=$(bluray_info "$device" "${keydb_args[@]}" --playlist "$playlist" --all --json)
            is_3d=$(jq -r '.bluray["3D content"] == true' <<<"$disc_metadata")
            format=mkv
            if [[ $is_3d == true ]]; then
                mvc_streams=$(probe_mvc "$playlist")
                if [[ -n $mvc_streams ]]; then
                    format=m2ts
                    echo "Playlist $playlist: verified MVC payload; preserving all streams as M2TS." >&2
                else
                    echo "Playlist $playlist: MVC not verified in sample; retaining MKV output." >&2
                fi
            fi
        fi
        playlist_formats[$playlist]=$format
        if [[ $format == m2ts ]]; then
            for selection in "${selected_stream_ids[@]}"; do
                if [[ ${selection%%:*} == "$playlist" ]]; then
                    echo "Playlist $playlist: original M2TS preserves all streams; remove --stream-id selections." >&2
                    exit 2
                fi
            done
        fi
        if [[ -n $output_file && $format == m2ts && ${output_file,,} != *.m2ts ]]; then
            echo "Playlist $playlist requires a .m2ts output filename or an existing output directory." >&2
            exit 2
        fi
        if [[ -n $output_file && $format == mkv && ${output_file,,} == *.m2ts ]]; then
            echo 'MKV output must not use an .m2ts filename; choose --output-format m2ts explicitly.' >&2
            exit 2
        fi
    done
    for playlist in "${selected_playlists[@]}"; do
        IFS= read -r -d '' playlist_output < <(playlist_output_path "$playlist")
        validate_output_filename "$playlist_output"
        if [[ ${playlist_formats[$playlist]} == m2ts ]]; then
            validate_output_filename "$playlist_output.chapters.txt"
        fi
        if [[ -e $playlist_output || -L $playlist_output ]]; then
            echo "Refusing to overwrite existing file: $playlist_output" >&2
            exit 1
        fi
        if [[ ${playlist_formats[$playlist]} == m2ts && (-e $playlist_output.chapters.txt || -L $playlist_output.chapters.txt) ]]; then
            echo "Refusing to overwrite existing file: $playlist_output.chapters.txt" >&2
            exit 1
        fi
        if [[ -v "seen_playlists[$playlist]" ]]; then
            echo "Duplicate playlist ID: $playlist" >&2
            exit 2
        fi
        seen_playlists[$playlist]=1
    done
    for selection in "${selected_stream_ids[@]}"; do
        if [[ ! -v "seen_playlists[${selection%%:*}]" ]]; then
            echo "Stream selection refers to an unselected playlist: $selection" >&2
            exit 2
        fi
    done
    if ! command -v bluray_copy >/dev/null; then
        echo 'Copying Blu-ray playlists requires bluray_copy.' >&2
        exit 1
    fi
    if [[ " ${playlist_formats[*]} " == *" mkv "* ]] && ! command -v ffmpeg >/dev/null; then
        echo 'Copying Blu-ray playlists to MKV requires ffmpeg.' >&2
        exit 1
    fi
    if ! command -v ffprobe >/dev/null; then
        echo 'Validating Blu-ray copies requires ffprobe.' >&2
        exit 1
    fi
    if ! command -v bluray_info >/dev/null || ! command -v python3 >/dev/null; then
        echo 'Importing Blu-ray chapters requires bluray_info and python3.' >&2
        exit 1
    fi
    for playlist in "${selected_playlists[@]}"; do
        IFS= read -r -d '' playlist_output < <(playlist_output_path "$playlist")
        map_args=()
        for selection in "${selected_stream_ids[@]}"; do
            if [[ ${selection%%:*} == "$playlist" ]]; then
                map_args+=(-map "0:i:${selection#*:}")
            fi
        done
        if [[ ${#map_args[@]} -eq 0 ]]; then
            map_args=(-map 0)
        fi
        make_temporary_workspace "$output_directory"
        chapter_metadata=$temporary_directory/chapters.ffmetadata
        chapter_sidecar=$temporary_directory/chapters.txt
        copy_size_file=$temporary_directory/copy-size
        chapter_count=$(bluray_info "$device" "${keydb_args[@]}" --playlist "$playlist" --chapters --json |
            PYTHONPATH="$module_directory:${PYTHONPATH:-}" \
                python3 -m dvdvideo.bluray_chapters "$playlist" "$chapter_metadata" "$chapter_sidecar" \
                    --size-file "$copy_size_file")
        expected_copy_bytes=$(<"$copy_size_file")
        echo "Playlist $playlist: imported $chapter_count chapter marker(s)." >&2
        attempt=0
        while true; do
            attempt=$((attempt + 1))
            rm -f "$temporary_copy" "$temporary_output"
            copy_valid=false
            if bluray_copy "$device" "${keydb_args[@]}" --playlist "$playlist" -o "$temporary_copy"; then
                if ! copied_bytes=$(stat -c '%s' -- "$temporary_copy"); then
                    echo "Playlist $playlist: could not determine copied size." >&2
                elif [[ $copied_bytes != "$expected_copy_bytes" ]]; then
                    echo "Playlist $playlist: incomplete copy (expected $expected_copy_bytes bytes, received $copied_bytes)." >&2
                elif [[ ${playlist_formats[$playlist]} == m2ts ]]; then
                    temporary_output=$temporary_copy
                    copy_valid=true
                elif ffmpeg -nostdin -y -xerror -i "$temporary_copy" -f ffmetadata -i "$chapter_metadata" \
                    "${map_args[@]}" -map_chapters 1 -c copy "$temporary_output"; then
                    copy_valid=true
                fi
            fi
            if [[ $copy_valid == true ]] \
                && [[ -s $temporary_output ]] \
                && ffprobe -v error "$temporary_output" >/dev/null; then
                break
            fi
            if (( attempt > retries )); then
                echo "Failed to produce a valid copy of playlist $playlist after $attempt attempt(s)." >&2
                exit 1
            fi
            echo "Retrying playlist $playlist (attempt $((attempt + 1)) of $((retries + 1)))..." >&2
        done
        temporary_copy=
        if [[ ${playlist_formats[$playlist]} == m2ts && $chapter_count -gt 0 ]]; then
            publish_output "$playlist_output" "$chapter_sidecar"
            echo "Chapters: $playlist_output.chapters.txt (playlist-relative markers; player support varies)" >&2
        else
            publish_output "$playlist_output"
        fi
        printf 'Copied: %s\n' "$playlist_output"
    done
    exit 0
fi

if [[ $list_streams == true ]]; then
    if [[ $# -lt 1 || $# -gt 2 ]]; then
        usage >&2
        exit 2
    fi
else
    if [[ $# -lt 2 || $# -gt 3 ]]; then
        usage >&2
        exit 2
    fi
fi

if ! command -v ffmpeg >/dev/null; then
    echo 'ffmpeg is required.' >&2
    exit 1
fi

if ! command -v ffprobe >/dev/null; then
    if [[ $list_streams == true ]]; then
        echo 'ffprobe is required to list streams.' >&2
    else
        echo 'ffprobe is required to validate the copied output.' >&2
    fi
    exit 1
fi

input=$1
if [[ $input == -* ]]; then
    input=./$input
fi
if [[ -d $input ]]; then
    IFS= read -r -d '' dvd_folder < <(basename -z -- "$input")
    if [[ ${dvd_folder^^} != VIDEO_TS ]]; then
        dvd_folders=()
        for candidate in "$input"/[Vv][Ii][Dd][Ee][Oo]_[Tt][Ss]; do
            if [[ -d $candidate ]]; then
                dvd_folders+=("$candidate")
            fi
        done
        if [[ ${#dvd_folders[@]} -eq 1 ]]; then
            input=${dvd_folders[0]}
        else
            echo "DVD directory must contain exactly one VIDEO_TS folder (case-insensitive): $input" >&2
            exit 2
        fi
    fi

elif ! [[ -f $input || -b $input ]]; then
    echo "DVD input must be an existing ISO file, block device, or VIDEO_TS directory: $input" >&2
    exit 2
fi

if [[ $list_streams == true ]]; then
    title_or_playlist=${2:-1}
else
    output=$2
    title_or_playlist=${3:-1}

    if [[ $output == -* ]]; then
        output=./$output
    fi
fi

if ! [[ $title_or_playlist =~ ^[1-9][0-9]*$ ]]; then
    echo 'TITLE_OR_PLAYLIST must be a positive integer.' >&2
    exit 2
fi

ffmpeg_demuxers=$(ffmpeg -hide_banner -demuxers 2>/dev/null)
if ! grep -q ' dvdvideo ' <<<"$ffmpeg_demuxers"; then
    echo 'ffmpeg must provide the dvdvideo input format; libdvdnav alone is not sufficient.' >&2
    exit 1
fi

input_args=(-f dvdvideo -title "$title_or_playlist" -i "$input")

if [[ $list_streams == true ]]; then
    stream_listing=$(ffprobe -v error "${input_args[@]}" \
        -show_entries stream=index,codec_type,codec_name,profile,channels,channel_layout:stream_tags=language,title \
        -of default=noprint_wrappers=0 | LC_ALL=C tr -d '\000-\010\013\014\016-\037' | awk '
        BEGIN { OFS = "\t"; print "INDEX", "TYPE", "CODEC", "PROFILE", "CH", "LAYOUT", "LANG", "TITLE" }
        /^\[STREAM\]/ { idx = type = codec = profile = ch = layout = lang = title = ""; next }
        /^\[\/STREAM\]/ {
            if (profile == "unknown") profile = ""
            print idx, type, codec, profile, ch, layout, lang, title
            next
        }
        /^\[/ { next }
        /^index=/          { v = $0; sub(/^index=/, "", v); idx = v; next }
        /^codec_type=/     { v = $0; sub(/^codec_type=/, "", v); type = v; next }
        /^codec_name=/     { v = $0; sub(/^codec_name=/, "", v); codec = v; next }
        /^profile=/        { v = $0; sub(/^profile=/, "", v); profile = v; next }
        /^channels=/       { v = $0; sub(/^channels=/, "", v); ch = v; next }
        /^channel_layout=/ { v = $0; sub(/^channel_layout=/, "", v); layout = v; next }
        /^TAG:language=/   { v = $0; sub(/^TAG:language=/, "", v); gsub(/\t/, " ", v); lang = v; next }
        /^TAG:title=/      { v = $0; sub(/^TAG:title=/, "", v); gsub(/\t/, " ", v); title = v; next }
    ')
    if [[ $list_streams_tsv == true ]]; then
        printf '%s\n' "$stream_listing"
    else
        printf '%s\n' "$stream_listing" | column -t -s $'\t'
    fi
    exit 0
fi

if [[ -e $output || -L $output ]]; then
    echo "Refusing to overwrite existing file: $output" >&2
    exit 1
fi

IFS= read -r -d '' output_directory < <(dirname -z -- "$output")
if ! [[ -d $output_directory ]]; then
    echo "Output directory does not exist: $output_directory" >&2
    exit 2
fi
validate_output_filename "$output"

map_args=()
for stream_index in "${selected_streams[@]}"; do
    map_args+=(-map "0:$stream_index")
done

if [[ ${#map_args[@]} -eq 0 ]]; then
    map_args=(-map 0)
fi

make_temporary_workspace "$output_directory"
attempt=0
while true; do
    attempt=$((attempt + 1))
    rm -f "$temporary_output"
    if ffmpeg -nostdin -y -xerror "${input_args[@]}" "${map_args[@]}" -c copy "$temporary_output" \
        && [[ -s $temporary_output ]] \
        && ffprobe -v error "$temporary_output" >/dev/null; then
        break
    fi
    if (( attempt > retries )); then
        echo "Failed to produce a valid copy of title $title_or_playlist after $attempt attempt(s)." >&2
        exit 1
    fi
    echo "Retrying title $title_or_playlist (attempt $((attempt + 1)) of $((retries + 1)))..." >&2
done
publish_output "$output"