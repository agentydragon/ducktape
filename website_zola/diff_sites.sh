#!/usr/bin/env bash
# Compare a Hakyll-generated site against a Zola-generated site.
# Accounts for URL structure differences (flat .html vs directory/index.html).
#
# Usage: diff_sites.sh <hakyll_dir> <zola_dir>

set -euo pipefail

HAKYLL="${1:?Usage: diff_sites.sh <hakyll_dir> <zola_dir>}"
ZOLA="${2:?Usage: diff_sites.sh <hakyll_dir> <zola_dir>}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

identical=0
differing=0
hakyll_only=0
zola_only=0

# Map a Hakyll relative path to the expected Zola relative path.
hakyll_to_zola_path() {
  local h="$1"
  case "$h" in
    # Top-level pages: foo.html -> foo/index.html
    about.html) echo "about/index.html" ;;
    found.html) echo "found/index.html" ;;
    nfc.html) echo "nfc/index.html" ;;
    nfc-armband.html) echo "nfc-armband/index.html" ;;
    archive.html) echo "archive/index.html" ;;
    # Posts: posts/slug.html -> posts/slug/index.html
    posts/*.html)
      local slug="${h%.html}"
      echo "${slug}/index.html"
      ;;
    # These stay the same
    index.html | atom.xml | rss.xml | robots.txt | sitemap.xml)
      echo "$h"
      ;;
    # CSS: Hakyll css/default.css -> Zola default.css (compiled SASS)
    css/default.css) echo "default.css" ;;
    css/default.scss) echo "__SKIP__" ;; # source file, not in Zola output
    # Everything else: try same path
    *) echo "$h" ;;
  esac
}

echo -e "${BOLD}=== Site Diff: Hakyll vs Zola ===${NC}"
echo -e "Hakyll: ${HAKYLL}"
echo -e "Zola:   ${ZOLA}"
echo

# --- Phase 1: Check Hakyll files against Zola ---
echo -e "${BOLD}--- Hakyll files ---${NC}"

while IFS= read -r hakyll_file; do
  rel="${hakyll_file#"$HAKYLL"/}"
  zola_rel=$(hakyll_to_zola_path "$rel")

  if [[ "$zola_rel" == "__SKIP__" ]]; then
    continue
  fi

  zola_file="${ZOLA}/${zola_rel}"

  if [[ ! -f "$zola_file" ]]; then
    echo -e "  ${YELLOW}HAKYLL-ONLY${NC}  ${rel}  (expected: ${zola_rel})"
    ((hakyll_only++)) || true
    continue
  fi

  # Binary or text comparison
  case "$rel" in
    *.html | *.xml | *.css | *.txt)
      if diff -q <(sed 's/[[:space:]]*$//' "$hakyll_file") \
        <(sed 's/[[:space:]]*$//' "$zola_file") &>/dev/null; then
        echo -e "  ${GREEN}IDENTICAL${NC}    ${rel}"
        ((identical++)) || true
      else
        echo -e "  ${RED}DIFFERENT${NC}    ${rel}  <->  ${zola_rel}"
        ((differing++)) || true
        # Show first 30 lines of diff
        diff -u \
          --label "hakyll/${rel}" \
          --label "zola/${zola_rel}" \
          <(sed 's/[[:space:]]*$//' "$hakyll_file") \
          <(sed 's/[[:space:]]*$//' "$zola_file") \
          | head -60 || true
        echo
      fi
      ;;
    *)
      if cmp -s "$hakyll_file" "$zola_file"; then
        echo -e "  ${GREEN}IDENTICAL${NC}    ${rel}"
        ((identical++)) || true
      else
        echo -e "  ${RED}DIFFERENT${NC}    ${rel}  <->  ${zola_rel}  (binary)"
        ((differing++)) || true
      fi
      ;;
  esac
done < <(find "$HAKYLL" -type f | sort)

# --- Phase 2: Check for Zola-only files ---
echo
echo -e "${BOLD}--- Zola-only files (not in Hakyll) ---${NC}"

# Build a set of expected Zola paths from Hakyll
declare -A expected_zola_paths
while IFS= read -r hakyll_file; do
  rel="${hakyll_file#"$HAKYLL"/}"
  zola_rel=$(hakyll_to_zola_path "$rel")
  if [[ "$zola_rel" != "__SKIP__" ]]; then
    expected_zola_paths["$zola_rel"]=1
  fi
done < <(find "$HAKYLL" -type f | sort)

while IFS= read -r zola_file; do
  rel="${zola_file#"$ZOLA"/}"
  if [[ -z "${expected_zola_paths[$rel]+x}" ]]; then
    echo -e "  ${CYAN}ZOLA-ONLY${NC}    ${rel}"
    ((zola_only++)) || true
  fi
done < <(find "$ZOLA" -type f | sort)

# --- Summary ---
echo
echo -e "${BOLD}=== Summary ===${NC}"
echo -e "  ${GREEN}Identical${NC}:    ${identical}"
echo -e "  ${RED}Different${NC}:    ${differing}"
echo -e "  ${YELLOW}Hakyll-only${NC}:  ${hakyll_only}"
echo -e "  ${CYAN}Zola-only${NC}:    ${zola_only}"

if ((differing > 0 || hakyll_only > 0)); then
  exit 1
fi
