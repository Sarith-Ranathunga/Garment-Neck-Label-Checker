#!/usr/bin/env python3
"""
Static checks on the Android source.

The Android SDK cannot be reached from the environment this was written in, so
`gradlew assembleDebug` cannot run here. These checks cover the failures that
would otherwise only surface as a red build in Android Studio — and they are
the failures that actually happen when a project is written in one sitting:
a view id that does not exist in the layout, a string resource referenced but
never defined, a Room query naming a column that was renamed, a dependency
alias with no entry in the version catalogue.

Run from the `android/` directory:  python3 tools/verify.py
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "app/src/main/java/lk/ac/nibm/gnlc"
RES = ROOT / "app/src/main/res"

problems: list[str] = []
notes: list[str] = []


def fail(msg: str) -> None:
    problems.append(msg)


def note(msg: str) -> None:
    notes.append(msg)


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


def strip_kotlin(text: str) -> str:
    """
    Remove comments and string literals from Kotlin source.

    A character scan, not a sequence of regexes. Stripping line comments with
    a regex first turns `"http://"` into the start of a comment and eats the
    rest of the line — which this checker did, and it reported a balanced file
    as broken. The lesson generalises: anything that has to tell code from
    text cannot do it one pattern at a time.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        three = text[i:i + 3]

        if three == '"""':
            end = text.find('"""', i + 3)
            out.append('""')
            i = (end + 3) if end != -1 else n
        elif two == "/*":
            end = text.find("*/", i + 2)
            out.append(" ")
            i = (end + 2) if end != -1 else n
        elif two == "//":
            end = text.find("\n", i)
            out.append(" ")
            i = end if end != -1 else n
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] in ('"', "\n"):
                    break
                j += 1
            out.append('""')
            i = j + 1
        elif c == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] in ("'", "\n"):
                    break
                j += 1
            out.append("''")
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


# ============================================================ resources =====

def load_resource_names() -> dict[str, set[str]]:
    """Every name defined under res/values, plus file-based resources."""
    defined: dict[str, set[str]] = {
        "string": set(), "color": set(), "style": set(), "dimen": set(),
        "drawable": set(), "mipmap": set(), "xml": set(), "layout": set(),
        "array": set(), "integer": set(), "bool": set(),
    }
    for path in (RES / "values").glob("*.xml"):
        root = ET.parse(path).getroot()
        for child in root:
            if not isinstance(child.tag, str):
                continue
            name = child.get("name")
            if name is None:
                continue
            kind = {"string-array": "array", "integer-array": "array"}.get(
                child.tag, child.tag)
            defined.setdefault(kind, set()).add(name)

    for folder, kind in (
        ("drawable", "drawable"),
        ("mipmap-anydpi-v26", "mipmap"),
        ("xml", "xml"),
        ("layout", "layout"),
    ):
        d = RES / folder
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    defined[kind].add(f.stem)
    return defined


DEFINED = load_resource_names()

# Styles supplied by Material Components / AppCompat, which this project
# references but does not define.
LIBRARY_STYLE = re.compile(
    r"^(Widget|Theme|TextAppearance|ThemeOverlay|Base|Platform|AlertDialog|"
    r"android:|Animation)\b")


def check_xml_resource_refs() -> None:
    """Every @type/name in a layout or drawable must resolve."""
    ref = re.compile(r'"@(\+?)(string|color|style|drawable|mipmap|xml|dimen|'
                     r'layout|integer|bool|array)/([A-Za-z0-9_.]+)"')
    for path in sorted(RES.rglob("*.xml")):
        text = path.read_text(encoding="utf-8")
        # Parse first: a malformed layout is an immediate build failure.
        try:
            ET.parse(path)
        except ET.ParseError as exc:
            fail(f"{rel(path)}: XML will not parse — {exc}")
            continue
        for plus, kind, name in ref.findall(text):
            if plus:          # @+id declarations
                continue
            if name in DEFINED.get(kind, set()):
                continue
            # Styles that come from Material Components or AppCompat are not
            # defined here and are not meant to be.
            if kind == "style" and LIBRARY_STYLE.match(name):
                continue
            fail(f"{rel(path)}: @{kind}/{name} is referenced but not defined")


def check_implicit_style_parents() -> None:
    """
    A dotted style name is implicit inheritance, and the implied parent must
    exist.

    `<style name="Gnlc.Label">` means "inherit from the style named Gnlc".
    If no such style exists, aapt2 fails with `resource style/Gnlc not
    found` — once per offending style, and the message names the *missing
    parent*, not the style that asked for it, so four errors all read
    identically and none of them points at the line to change.

    An explicit `parent` attribute, including `parent=""`, opts out.

    This check exists because the first build of this project failed exactly
    this way, on four styles, after the static checks had passed.
    """
    defined_styles: set[str] = set()
    declarations: list[tuple[Path, str, str | None]] = []

    for path in sorted((RES / "values").glob("*.xml")):
        root = ET.parse(path).getroot()
        for child in root:
            if child.tag != "style":
                continue
            name = child.get("name")
            if name is None:
                continue
            defined_styles.add(name)
            declarations.append((path, name, child.get("parent")))

    for path, name, parent in declarations:
        if parent is not None:          # explicit, including parent=""
            continue
        if "." not in name:
            continue
        implied = name.rsplit(".", 1)[0]
        if implied in defined_styles:
            continue
        if LIBRARY_STYLE.match(implied):
            continue                    # e.g. Widget.Foo.Bar from Material
        fail(f'{rel(path)}: style "{name}" has no parent attribute, so it '
             f'implicitly inherits from "{implied}", which does not exist. '
             f'aapt2 will report: resource style/{implied} not found. '
             f'Add parent="" to opt out, or name an explicit parent')


def check_buildconfig_optin() -> None:
    """
    Using BuildConfig requires asking for it, since AGP 8.0.

    AGP 8.0 changed `android.defaults.buildfeatures.buildconfig` to false, so
    `BuildConfig` is not generated unless the module opts in. Without the
    opt-in the failure is an unresolved reference at the Kotlin step — which
    only appears after resource linking has succeeded, so it hides behind any
    resource error and costs a second full build to discover.
    """
    users = [p for p in sorted(SRC.rglob("*.kt"))
             if re.search(r"\bBuildConfig\.", strip_kotlin(
                 p.read_text(encoding="utf-8")))]
    if not users:
        return

    gradle = (ROOT / "app/build.gradle.kts").read_text(encoding="utf-8")
    if not re.search(r"buildConfig\s*=\s*true", gradle):
        names = ", ".join(rel(p) for p in users)
        fail(f"{names} use BuildConfig, but app/build.gradle.kts does not set "
             f"buildFeatures {{ buildConfig = true }}. AGP 8.0 stopped "
             f"generating BuildConfig by default, so this is an unresolved "
             f"reference at compile time")


def check_kotlin_resource_refs() -> None:
    """Every R.string.x / R.color.x / R.layout.x in Kotlin must resolve."""
    # (?<![.\w]) so that `android.R.color.transparent` and the Material
    # library's own R class are not mistaken for this project's resources.
    ref = re.compile(r"(?<![.\w])R\.(string|color|layout|drawable|style|dimen|"
                     r"mipmap|xml|integer|bool|array)\.([A-Za-z0-9_]+)")
    for path in sorted(SRC.rglob("*.kt")):
        text = path.read_text(encoding="utf-8")
        for kind, name in ref.findall(text):
            # Exclude the qualified Material / android R classes.
            if name not in DEFINED.get(kind, set()):
                fail(f"{rel(path)}: R.{kind}.{name} does not exist in res/")


# ======================================================== view binding =====

def layout_ids(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r'android:id="@\+id/([A-Za-z0-9_]+)"', text))


def binding_class_to_layout(name: str) -> str:
    """ActivitySignInBinding -> activity_sign_in"""
    base = name[: -len("Binding")]
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower()
    return snake


def check_view_binding() -> None:
    """
    Every `b.someView` must be an id in the layout that binding belongs to.

    This is the single most common way a hand-written Android screen fails to
    compile, and the error message Gradle gives for it points at the Kotlin
    line rather than at the missing id.
    """
    layouts = {p.stem: layout_ids(p) for p in (RES / "layout").glob("*.xml")}

    for path in sorted(SRC.rglob("*.kt")):
        text = path.read_text(encoding="utf-8")
        bindings = set(re.findall(r"\b([A-Z][A-Za-z0-9]*Binding)\b", text))
        if not bindings:
            continue

        # Which binding is bound to the local named `b` / `v` / `dialogBinding`.
        for binding in bindings:
            layout = binding_class_to_layout(binding)
            if layout not in layouts:
                fail(f"{rel(path)}: {binding} implies res/layout/{layout}.xml, "
                     f"which does not exist")
                continue

        # Collect every receiver variable assigned from a binding inflate.
        receivers: dict[str, str] = {}
        for var, binding in re.findall(
            r"\b(?:val|var)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
            r"([A-Z][A-Za-z0-9]*Binding)\.inflate", text
        ):
            receivers[var] = binding
        # `private lateinit var b: ActivityXBinding`
        for var, binding in re.findall(
            r"\b(?:lateinit\s+var|val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
            r"([A-Z][A-Za-z0-9]*Binding)\b", text
        ):
            receivers[var] = binding
        # `class Row(private val v: ItemQueueBinding)`
        for var, binding in re.findall(
            r"\b(?:private\s+)?val\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
            r"([A-Z][A-Za-z0-9]*Binding)\b", text
        ):
            receivers[var] = binding

        for var, binding in receivers.items():
            layout = binding_class_to_layout(binding)
            ids = layouts.get(layout)
            if ids is None:
                continue
            used = set(re.findall(
                rf"\b{re.escape(var)}\.([a-z][A-Za-z0-9_]*)\b", text))
            for prop in sorted(used):
                if prop in ids:
                    continue
                if prop == "root":          # always generated
                    continue
                fail(f"{rel(path)}: {var}.{prop} — no @+id/{prop} in "
                     f"res/layout/{layout}.xml")


# ================================================================= Room =====

KOTLIN_TO_SQL_TABLE = {}


def entity_fields() -> dict[str, set[str]]:
    """table name -> column names, read from the @Entity data classes."""
    text = (SRC / "data/Entities.kt").read_text(encoding="utf-8")
    tables: dict[str, set[str]] = {}

    # Split on each @Entity block.
    blocks = re.split(r"@Entity", text)[1:]
    for block in blocks:
        m = re.search(r'tableName\s*=\s*"([A-Za-z0-9_]+)"', block)
        if not m:
            continue
        table = m.group(1)
        # The constructor parameter list of the data class that follows.
        cls = re.search(r"data class \w+\((.*?)\n\)", block, re.S)
        if not cls:
            continue
        fields = set(re.findall(
            r"(?:^|\n)\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:val|var)\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)\s*:", cls.group(1)))
        tables[table] = fields
    return tables


SQL_WORDS = {
    "select", "from", "where", "and", "or", "not", "in", "is", "null", "order",
    "by", "asc", "desc", "limit", "count", "update", "set", "delete", "insert",
    "into", "values", "group", "having", "join", "left", "inner", "on", "as",
    "distinct", "coalesce", "sum", "max", "min", "avg", "case", "when", "then",
    "else", "end", "like", "between", "exists", "union", "all", "replace",
}


def check_room_queries() -> None:
    """
    Every column named in an @Query must exist on the entity, and every
    `:parameter` must exist on the function.

    Room checks both at compile time, and its error is a wall of generated
    Java. Catching it here points at the line instead.
    """
    tables = entity_fields()
    if not tables:
        fail("could not read any @Entity tables from data/Entities.kt")
        return
    note(f"Room entities: " + ", ".join(
        f"{t} ({len(c)} cols)" for t, c in sorted(tables.items())))

    dao = (SRC / "data/Dao.kt").read_text(encoding="utf-8")

    # Each @Query(...) followed by its function signature.
    pattern = re.compile(
        r'@Query\(\s*(?:"""(?P<triple>.*?)"""|(?P<single>(?:"[^"]*"\s*(?:\+\s*)?)+))'
        r'\s*\)\s*suspend fun (?P<fn>\w+)\((?P<params>[^)]*)\)',
        re.S)

    found = 0
    for m in pattern.finditer(dao):
        found += 1
        sql = m.group("triple")
        if sql is None:
            sql = "".join(re.findall(r'"([^"]*)"', m.group("single")))
        fn = m.group("fn")
        params = set(re.findall(r"(\w+)\s*:", m.group("params")))

        # --- bind parameters ---
        for bind in set(re.findall(r":(\w+)", sql)):
            if bind not in params:
                fail(f"Dao.kt {fn}(): query binds :{bind} but the function "
                     f"has no such parameter")

        # --- table names ---
        used_tables = set(re.findall(r"\bFROM\s+(\w+)", sql, re.I)) \
            | set(re.findall(r"\bJOIN\s+(\w+)", sql, re.I)) \
            | set(re.findall(r"\bUPDATE\s+(\w+)", sql, re.I)) \
            | set(re.findall(r"\bDELETE\s+FROM\s+(\w+)", sql, re.I))
        for t in used_tables:
            if t not in tables:
                fail(f"Dao.kt {fn}(): unknown table {t!r}")

        known_cols: set[str] = set()
        for t in used_tables:
            known_cols |= tables.get(t, set())
        if not known_cols:
            continue

        # --- bare identifiers that ought to be columns ---
        stripped = re.sub(r":\w+", " ? ", sql)
        stripped = re.sub(r"'[^']*'", " ? ", stripped)
        for ident in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", stripped):
            low = ident.lower()
            if low in SQL_WORDS or ident in used_tables:
                continue
            if ident in known_cols:
                continue
            fail(f"Dao.kt {fn}(): {ident!r} is not a column on "
                 f"{'/'.join(sorted(used_tables))}")

    note(f"Room @Query statements checked: {found}")

    # Every entity in @Database must be declared in Entities.kt.
    db = (SRC / "data/Db.kt").read_text(encoding="utf-8")
    declared = set(re.findall(r"(\w+Entity)::class", db))
    ent_src = (SRC / "data/Entities.kt").read_text(encoding="utf-8")
    actual = set(re.findall(r"data class (\w+Entity)\b", ent_src))
    for name in sorted(declared - actual):
        fail(f"Db.kt lists {name} but no such data class exists")
    for name in sorted(actual - declared):
        fail(f"Entities.kt defines {name} but Db.kt does not list it "
             f"— Room will not create its table")


# ========================================================= dependencies =====

def check_version_catalogue() -> None:
    """Every libs.* alias used in build.gradle.kts must exist in the catalogue."""
    toml = (ROOT / "gradle/libs.versions.toml").read_text(encoding="utf-8")

    libs = set()
    plugins = set()
    section = None
    for line in toml.splitlines():
        s = line.strip()
        if s.startswith("["):
            section = s.strip("[]")
            continue
        if not s or s.startswith("#") or "=" not in s:
            continue
        name = s.split("=", 1)[0].strip()
        if section == "libraries":
            libs.add(name.replace("-", "."))
        elif section == "plugins":
            plugins.add(name.replace("-", "."))
        elif section == "versions":
            pass

    # Versions referenced by version.ref must exist.
    versions = set()
    section = None
    for line in toml.splitlines():
        s = line.strip()
        if s.startswith("["):
            section = s.strip("[]"); continue
        if section == "versions" and "=" in s and not s.startswith("#"):
            versions.add(s.split("=", 1)[0].strip())
    for ref in set(re.findall(r'version\.ref\s*=\s*"([^"]+)"', toml)):
        if ref not in versions:
            fail(f"libs.versions.toml: version.ref = \"{ref}\" has no entry "
                 f"under [versions]")

    for gradle in (ROOT / "app/build.gradle.kts", ROOT / "build.gradle.kts"):
        text = gradle.read_text(encoding="utf-8")
        for alias in set(re.findall(r"\blibs\.plugins\.([A-Za-z0-9.]+)", text)):
            if alias.rstrip(".") not in plugins:
                fail(f"{rel(gradle)}: libs.plugins.{alias} not in the catalogue")
        for alias in set(re.findall(r"\blibs\.((?!plugins\.)[A-Za-z0-9.]+)", text)):
            if alias.rstrip(".") not in libs:
                fail(f"{rel(gradle)}: libs.{alias} not in the catalogue")

    note(f"catalogue: {len(libs)} libraries, {len(plugins)} plugins, "
         f"{len(versions)} pinned versions")


# ============================================================= manifest =====

def check_manifest() -> None:
    path = ROOT / "app/src/main/AndroidManifest.xml"
    text = path.read_text(encoding="utf-8")
    try:
        ET.parse(path)
    except ET.ParseError as exc:
        fail(f"AndroidManifest.xml will not parse — {exc}")
        return

    pkg_root = SRC
    for name in set(re.findall(r'android:name="(\.[\w.]+)"', text)):
        parts = name.lstrip(".").split(".")
        candidate = pkg_root.joinpath(*parts[:-1]) / f"{parts[-1]}.kt"
        if not candidate.exists():
            fail(f"AndroidManifest.xml declares {name} but "
                 f"{rel(candidate)} does not exist")

    declared = set(re.findall(r'android:name="\.ui\.(\w+)"', text))
    on_disk = {p.stem for p in (SRC / "ui").glob("*Activity.kt")}
    for missing in sorted(on_disk - declared):
        fail(f"{missing} exists but is not declared in AndroidManifest.xml "
             f"— starting it would crash")

    if "android.permission.CAMERA" not in text:
        fail("AndroidManifest.xml: the CAMERA permission is missing")
    if "android.permission.INTERNET" not in text:
        fail("AndroidManifest.xml: the INTERNET permission is missing")
    note(f"manifest: {len(declared)} activities, all present on disk")


# =============================================================== Kotlin =====

def check_kotlin_balance() -> None:
    """Brace, paren and bracket balance, ignoring strings and comments."""
    for path in sorted(SRC.rglob("*.kt")):
        t = strip_kotlin(path.read_text(encoding="utf-8"))
        for open_c, close_c, label in (("{", "}", "brace"),
                                       ("(", ")", "paren"),
                                       ("[", "]", "bracket")):
            if t.count(open_c) != t.count(close_c):
                fail(f"{rel(path)}: unbalanced {label}s "
                     f"({t.count(open_c)} {open_c} vs {t.count(close_c)} {close_c})")


def check_imports() -> None:
    """
    Simple names used but never imported or defined locally.

    Only checks a list of types this project actually uses, because a general
    resolver would need the whole classpath. These are the ones a hand-written
    file forgets.
    """
    watch = {
        "ContextCompat": "androidx.core.content.ContextCompat",
        "AlertDialog": "androidx.appcompat.app.AlertDialog",
        "MaterialButton": "com.google.android.material.button.MaterialButton",
        "LinearLayoutManager": "androidx.recyclerview.widget.LinearLayoutManager",
        "RecyclerView": "androidx.recyclerview.widget.RecyclerView",
        "lifecycleScope": "androidx.lifecycle.lifecycleScope",
        "launch": "kotlinx.coroutines.launch",
        "withContext": "kotlinx.coroutines.withContext",
        "Dispatchers": "kotlinx.coroutines.Dispatchers",
        "GridLayout": "android.widget.GridLayout",
        "LinearLayout": "android.widget.LinearLayout",
        "TextView": "android.widget.TextView",
        "Bitmap": "android.graphics.Bitmap",
        "BitmapFactory": "android.graphics.BitmapFactory",
        "Matrix": "android.graphics.Matrix",
        "Rect": "android.graphics.Rect",
        "Color": "android.graphics.Color",
        "VibrationEffect": "android.os.VibrationEffect",
        "Vibrator": "android.os.Vibrator",
        "Build": "android.os.Build",
        "View": "android.view.View",
        "ViewGroup": "android.view.ViewGroup",
        "LayoutInflater": "android.view.LayoutInflater",
        "Gravity": "android.view.Gravity",
        "Intent": "android.content.Intent",
        "WindowManager": "android.view.WindowManager",
        "File": "java.io.File",
        "UUID": "java.util.UUID",
        "Executors": "java.util.concurrent.Executors",
        "ExecutorService": "java.util.concurrent.ExecutorService",
        "AtomicBoolean": "java.util.concurrent.atomic.AtomicBoolean",
        "TimeUnit": "java.util.concurrent.TimeUnit",
        "ByteArrayOutputStream": "java.io.ByteArrayOutputStream",
        "Log": "android.util.Log",
        "Gson": "com.google.gson.Gson",
        "MultipartBody": "okhttp3.MultipartBody",
        "OkHttpClient": "okhttp3.OkHttpClient",
        "Retrofit": "retrofit2.Retrofit",
        "InputImage": "com.google.mlkit.vision.common.InputImage",
        "BarcodeScanning": "com.google.mlkit.vision.barcode.BarcodeScanning",
        "BarcodeScannerOptions":
            "com.google.mlkit.vision.barcode.BarcodeScannerOptions",
        "BarcodeScanner": "com.google.mlkit.vision.barcode.BarcodeScanner",
        "Barcode": "com.google.mlkit.vision.barcode.common.Barcode",
        "ImageCapture": "androidx.camera.core.ImageCapture",
        "ImageAnalysis": "androidx.camera.core.ImageAnalysis",
        "ImageProxy": "androidx.camera.core.ImageProxy",
        "Preview": "androidx.camera.core.Preview",
        "CameraSelector": "androidx.camera.core.CameraSelector",
        "ProcessCameraProvider": "androidx.camera.lifecycle.ProcessCameraProvider",
        "ImageCaptureException": "androidx.camera.core.ImageCaptureException",
        "ExperimentalGetImage": "androidx.camera.core.ExperimentalGetImage",
        "ActivityResultContracts":
            "androidx.activity.result.contract.ActivityResultContracts",
        "AppCompatActivity": "androidx.appcompat.app.AppCompatActivity",
        "WorkManager": "androidx.work.WorkManager",
        "Constraints": "androidx.work.Constraints",
        "NetworkType": "androidx.work.NetworkType",
        "BackoffPolicy": "androidx.work.BackoffPolicy",
        "CoroutineWorker": "androidx.work.CoroutineWorker",
        "WorkerParameters": "androidx.work.WorkerParameters",
        "SyncScheduler": "lk.ac.nibm.gnlc.work.SyncScheduler",
        "ScanRepository": "lk.ac.nibm.gnlc.data.ScanRepository",
        "ApiProvider": "lk.ac.nibm.gnlc.net.ApiProvider",
        "Prefs": "lk.ac.nibm.gnlc.util.Prefs",
        "Time": "lk.ac.nibm.gnlc.util.Time",
        "SyncState": "lk.ac.nibm.gnlc.data.SyncState",
        "ScanEntity": "lk.ac.nibm.gnlc.data.ScanEntity",
        "Db": "lk.ac.nibm.gnlc.data.Db",
        "R": "lk.ac.nibm.gnlc.R",
    }

    for path in sorted(SRC.rglob("*.kt")):
        text = path.read_text(encoding="utf-8")
        body = re.sub(r"^package .*$|^import .*$", "", text, flags=re.M)
        body = strip_kotlin(body)
        imports = set(re.findall(r"^import\s+([\w.]+)", text, re.M))
        declared_here = set(re.findall(
            r"^(?:\s*)(?:data |sealed |abstract |open |private |internal )*"
            r"(?:class|object|interface|enum class|fun)\s+(\w+)", body, re.M))
        same_package_file = path.parent

        for name, fqn in watch.items():
            # Used as a bare simple name?
            if not re.search(rf"\b{re.escape(name)}\b", body):
                continue
            if fqn in imports:
                continue
            # Fully qualified at the usage site is fine.
            if re.search(rf"\b{re.escape(fqn)}\b", body):
                continue
            if name in declared_here:
                continue
            # Same package, no import needed.
            pkg = re.search(r"^package\s+([\w.]+)", text, re.M)
            if pkg and fqn.rsplit(".", 1)[0] == pkg.group(1):
                continue
            # Defined in a sibling file of the same package?
            if (same_package_file / f"{name}.kt").exists():
                continue
            sibling_decl = any(
                re.search(rf"\b(?:class|object|interface|enum class)\s+{name}\b",
                          sib.read_text(encoding="utf-8"))
                for sib in same_package_file.glob("*.kt")
            )
            if sibling_decl:
                continue
            fail(f"{rel(path)}: uses {name} but does not import {fqn}")


def check_unused_imports() -> None:
    """An unused import is a warning, not an error — reported as a note."""
    count = 0
    for path in sorted(SRC.rglob("*.kt")):
        text = path.read_text(encoding="utf-8")
        body = re.sub(r"^import .*$", "", text, flags=re.M)
        for imp in re.findall(r"^import\s+([\w.]+)(?:\s+as\s+(\w+))?", text, re.M):
            fqn, alias = imp
            simple = alias or fqn.rsplit(".", 1)[-1]
            if simple == "*":
                continue
            if not re.search(rf"\b{re.escape(simple)}\b", body):
                note(f"{rel(path)}: import {fqn} appears unused")
                count += 1
    if count:
        note(f"{count} possibly-unused imports (warnings, not errors)")


# ================================================= the research invariants ==

def check_research_invariants() -> None:
    """
    The handful of properties the measurements depend on.

    These are not style rules. Each one, if broken, produces a number in
    Chapter 5 that looks valid and is not — which is the failure that does not
    announce itself.
    """
    repo = (SRC / "data/ScanRepository.kt").read_text(encoding="utf-8")

    # 1. Room insert must precede the network call inside capture().
    capture = re.search(r"suspend fun capture\((.*?)\n    }", repo, re.S)
    if not capture:
        fail("ScanRepository.capture() not found")
    else:
        body = capture.group(1)
        insert_at = body.find("db.scans().insert")
        send_at = body.find("trySend")
        if insert_at < 0:
            fail("capture() does not write to Room at all")
        elif send_at >= 0 and send_at < insert_at:
            fail("capture() posts to the network BEFORE writing to Room — "
                 "a scan taken during an outage would be lost")

    # 2. The bypass path must never carry a recognition id.
    bypass = re.search(r"suspend fun bypass\((.*?)\n    }", repo, re.S)
    if not bypass:
        fail("ScanRepository.bypass() not found")
    elif "recognitionResultId = null" not in bypass.group(1):
        fail("bypass() does not set recognitionResultId = null — the DP3 "
             "measurement in §5.8 would be corrupted")

    # 3. The fallback path's recognition id must be non-nullable.
    if not re.search(r"suspend fun manualFallback\([^)]*recognitionResultId:\s*Long\s*,",
                     repo, re.S):
        fail("manualFallback() should take a non-null Long recognitionResultId, "
             "so it cannot be called with null from somewhere else")

    # 4. local_id is reused on retry, never regenerated in trySend.
    send = re.search(r"suspend fun trySend\((.*?)\n    }", repo, re.S)
    if send and "UUID.randomUUID" in send.group(1):
        fail("trySend() generates a new UUID — a retry would be counted as a "
             "second garment instead of being deduplicated")

    # 5. The queue must not be presented as a success anywhere.
    capture_act = (SRC / "ui/CaptureActivity.kt").read_text(encoding="utf-8")
    if "verdict_queued" not in capture_act:
        fail("CaptureActivity does not use the distinct queued styling — a "
             "queued scan must not look like a verified one")

    # 6. Destructive migration must stay off: the queue holds unsent evidence.
    db = strip_kotlin((SRC / "data/Db.kt").read_text(encoding="utf-8"))
    if "fallbackToDestructiveMigration" in db:
        fail("Db.kt enables fallbackToDestructiveMigration — a schema change "
             "would wipe scans that have not reached the host")

    note("research invariants: Room-before-network, null-bypass, "
         "non-null fallback, local_id reuse, queued styling, no destructive "
         "migration")


# ================================================================= server ==

def check_api_surface() -> None:
    """Every endpoint the client calls must exist in the FastAPI service."""
    server = ROOT.parent / "server/app"
    if not server.is_dir():
        note("server/ not present; skipping the endpoint cross-check")
        return

    routes: set[tuple[str, str]] = set()
    for path in server.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        prefix = ""
        m = re.search(r'APIRouter\(prefix="([^"]*)"', text)
        if m:
            prefix = m.group(1)
        for verb, route in re.findall(
            r'@(?:app|router)\.(get|post|put|delete)\(\s*"([^"]+)"', text
        ):
            routes.add((verb.upper(), prefix + route))

    api = (SRC / "net/Api.kt").read_text(encoding="utf-8")
    calls = re.findall(r'@(GET|POST|PUT|DELETE)\("([^"]+)"\)', api)

    def normalise(p: str) -> str:
        p = "/" + p.lstrip("/")
        return re.sub(r"\{[^}]+\}", "{}", p)

    server_norm = {(v, normalise(p)) for v, p in routes}
    note(f"server exposes {len(server_norm)} routes")

    for verb, path in calls:
        if (verb, normalise(path)) not in server_norm:
            fail(f"Api.kt calls {verb} {path}, which the server does not expose")


# =================================================================== main ==

def main() -> int:
    check_xml_resource_refs()
    check_implicit_style_parents()
    check_buildconfig_optin()
    check_kotlin_resource_refs()
    check_view_binding()
    check_room_queries()
    check_version_catalogue()
    check_manifest()
    check_kotlin_balance()
    check_imports()
    check_unused_imports()
    check_research_invariants()
    check_api_surface()

    kt = list(SRC.rglob("*.kt"))
    xml = list((RES / "layout").glob("*.xml"))
    print(f"Checked {len(kt)} Kotlin files and {len(xml)} layouts.\n")

    for n in notes:
        print(f"  note  {n}")
    if notes:
        print()

    if problems:
        print(f"{len(problems)} problem(s):\n")
        for p in problems:
            print(f"  FAIL  {p}")
        return 1

    print("No problems found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
