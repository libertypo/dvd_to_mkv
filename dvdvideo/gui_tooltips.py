# @copyright: 2026 libertypo
# @license: GNU GPL-3

"""English GTK tooltip messages, marked for gettext extraction with -kN_."""

from dvdvideo.gui_translation import identity as N_

TOOLTIPS = {
    "dvd": N_("Use DVD mode to inspect and copy one title at a time."),
    "bluray": N_("Use Blu-ray mode to inspect and copy playlists."),
    "source_file": N_("Open a disc image as the source."),
    "source_folder": N_(
        "Use a folder containing a DVD or Blu-ray as the source."
    ),
    "drives": N_("Show detected optical drives and scan for changes."),
    "select_drive": N_("Use the disc in this drive as the source."),
    "refresh_drives": N_("Scan again for optical drives and inserted discs."),
    "output": N_("Choose where the copied video will be saved."),
    "manage_profiles": N_(
        "Create or edit reusable language and output settings."
    ),
    "details": N_("View reported disc or playlist metadata without copying."),
    "verify_copy": N_("Compare a saved copy with the original source."),
    "inspect": N_(
        "List tracks for the selected DVD title or playlists on the Blu-ray."
    ),
    "copy": N_("Review the selected content and output paths before copying."),
    "select_all": N_(
        "Select all inspected DVD tracks or Blu-ray playlists, including"
        " hidden items."
    ),
    "inspect_tracks": N_(
        "List the video, audio, and subtitle tracks in one selected Blu-ray"
        " playlist."
    ),
    "select_all_tracks": N_(
        "Include every inspected track from the playlist, including hidden"
        " tracks."
    ),
    "cancel_operation": N_("Request cancellation of the current operation."),
    "select_item": N_("Include this item in the copy selection."),
    "existing_copy": N_("Choose the MKV or M2TS file to verify."),
    "start_copy": N_("Start copying with the settings shown in the summary."),
    "cancel_copy": N_("Return to the main window without starting the copy."),
    "close_details": N_(
        "Return to the main window without changing the selection."
    ),
    "cancel_verify": N_(
        "Return to the main window without verifying the copy."
    ),
    "start_verify": N_(
        "Reread the source and compare it with the saved copy."
    ),
    "close_profiles": N_("Close the editor without saving any pending edits."),
    "save_profile": N_(
        "Save the current profile settings and apply the profile."
    ),
    "new_profile": N_("Clear the form to create a new profile."),
    "delete_profile": N_("Remove the selected custom profile."),
    "preserve_all": N_(
        "Copy the playlist output without filtering; 3D preservation is not"
        " guaranteed."
    ),
    "keep_selection": N_("Keep the current filtered track selection."),
    "close_loading": N_(
        "Close the application without waiting for preferences to load."
    ),
    "skip_loading": N_("Continue without saving settings for this session."),
    "wait_loading": N_("Keep waiting for preferences to load."),
    "stay_open": N_("Keep the application open."),
    "retry_save": N_("Try saving the pending settings again."),
    "confirm_close": N_(
        "Close the application, stopping any active operation."
    ),
    "wait_saving": N_("Keep waiting for the current save to finish."),
    "close_anyway": N_("Close now even if pending settings may be lost."),
    "discard_settings": N_("Close without saving the pending settings."),
    "discover_titles": N_(
        "List DVD titles using lsdvd. An extra same-language stereo audio"
        " track can suggest commentary but is unverified. Duration and track"
        " counts are hints, not automatic selection."
    ),
    "show_unselected": N_(
        "Show unselected loaded items without changing the copy selection."
    ),
    "hide_unselected": N_(
        "Hide unselected items without changing the copy selection."
    ),
    "preview": N_(
        "Play the selected DVD title or one selected Blu-ray playlist in mpv"
        " (2D). Preview uses the player's tracks, not the copy selections."
    ),
    "clear_all": N_(
        "Reset source, output, selections, and Activity. Saved profiles and"
        " choices are kept."
    ),
}
