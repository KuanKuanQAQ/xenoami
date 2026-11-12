#!/bin/sh
# Rebuild slides.pptx from the hand-drawn SVGs, notes.json and build.js.
# Needs: node + pptxgenjs (NODE_PATH), PPTX_SKILL (for apply_theme.js),
# snap firefox (renders the PNG fallback that old PowerPoint/LibreOffice show).
set -e
D=$(cd "$(dirname "$0")" && pwd)
R=$HOME/snap/firefox/common/claude-render; mkdir -p "$R/prof"
node "$D/build.js" "$D"
T=$(mktemp -d); (cd "$T" && unzip -q "$D/slides.pptx")
i=1
for n in fig1-concepts fig2-closures fig4-xbuf-orig fig5-xbuf-new; do
	cp "$D/$n.svg" "$R/"
	W=$(grep -o 'width="[0-9]*"' "$D/$n.svg" | head -1 | tr -dc 0-9)
	H=$(grep -o 'height="[0-9]*"' "$D/$n.svg" | head -1 | tr -dc 0-9)
	printf '<html><body style="margin:0"><img src="%s.svg" width="%s" height="%s" style="display:block"></body></html>' \
		$n $((W*3)) $((H*3)) > "$R/w-$n.html"
	firefox --headless --profile "$R/prof" --screenshot "$R/$n.png" \
		--window-size=$((W*3)),$((H*3)) "file://$R/w-$n.html" >/dev/null 2>&1
	cp "$R/$n.png" "$T/ppt/media/image-$i-1.png"
	i=$((i+1))
done
rm -f "$D/slides.pptx"; (cd "$T" && zip -qXrD "$D/slides.pptx" .); rm -rf "$T"
echo "wrote $D/slides.pptx"
