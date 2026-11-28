"""Legal notice bundled as text so the GUI need not read a file when opened."""

NOTICE = """# Legal notice and warranty disclaimer

Unfortunately, we cannot dispense with the legalese, so here is the whole
shebang. Please read it before using the software.

This project's intended purpose is **<u>personal use</u>**: backing up DVDs and Blu-rays
that you are legally entitled to copy. **<u>It is not intended for unauthorized copying,
sharing, or distribution of copyrighted material.</u>** Ownership of a disc does not
necessarily authorize copying or circumventing access controls; applicable laws
vary by jurisdiction. You are responsible for ensuring that your use complies
with applicable law and any necessary permissions.

The project also supports study of disc structures, metadata, and media-processing
software using material that may lawfully be examined. Describing an activity as
study, research, or educational use does not, by itself, authorize copying or
circumvention; any applicable legal exception must satisfy its own conditions.

A private-copying exception or payment of a private-copy levy does not, by
itself, authorize circumvention of technological protection measures. Permission
to copy content and permission to access or decrypt protected content are
separate questions under applicable law.

The application can invoke third-party tools and libraries, including tools
that accept a user-supplied decryption key database. Their availability,
open-source licensing, or use by this application does not establish that every
operation is lawful. Applicable third-party license terms must also be respected.

This notice grants no permission to copy copyrighted content or circumvent
protection, and does not establish that any particular use is lawful. If you
are unsure whether a use is permitted, obtain qualified legal advice before
proceeding.

## Independence and trademarks

This project is independent. It is not affiliated with or endorsed by MakeMKV,
FFmpeg, libbluray, AACS LA, the DVD Copy Control Association, the Blu-ray Disc
Association, or any disc publisher. All product names and trademarks belong to
their owners and are used only to describe compatibility.

## No keys or circumvention material

This repository does not include decryption keys, key databases, or copyrighted
media. Any key database you use is supplied by you, and you are responsible for
having the right to use it.

## Third-party software

External tools such as FFmpeg, FFprobe, libbluray utilities, `jq` and `7z` are
not bundled. They are installed separately and remain under their own licenses.
See [THIRD-PARTY.md](./THIRD-PARTY.md).

## Privacy

The application makes no network connections and sends no data anywhere. It
stores preferences locally under `$XDG_CONFIG_HOME` (or `~/.config`).

The software is provided **"AS IS", WITHOUT ANY WARRANTY**, including implied
warranties of merchantability or fitness for a particular purpose, to the extent
permitted by applicable law. Inspection results are advisory; correct title
identification, complete backups, and preservation of all disc features are not
guaranteed. Use the software at your own risk.

The warranty disclaimer and limitation of liability are governed by sections 15
and 16 of the [GNU General Public License](./LICENSE). This notice describes the
project's intended purpose and does not impose additional restrictions on the
rights granted by that license. It is not legal advice.

## Rights-holder concerns

If you believe this repository contains material that infringes your rights,
please contact the maintainer, identifying the material, where it is located
in this repository, and the basis of your concern. We aim to review notices
promptly and will remove material that is shown to infringe. Do not include confidential information or personal data in public
issues.
"""
