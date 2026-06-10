#!/bin/bash
# PDX Onsite — release script
# Usage: ./release.sh 2.1.0 "What changed in this release"

set -e

VERSION=$1
NOTES=$2
EXE="$HOME/Desktop/PDX_Onsite.exe"

if [ -z "$VERSION" ] || [ -z "$NOTES" ]; then
  echo "Usage: ./release.sh <version> \"release notes\""
  echo "Example: ./release.sh 2.1.0 \"Fixed receipt printing, added logo upload\""
  exit 1
fi

if [ ! -f "$EXE" ]; then
  echo "❌  PDX_Onsite.exe not found on Desktop."
  echo "    Copy it from Parallels (dist\\PDX_Onsite.exe) to your Mac Desktop first."
  exit 1
fi

echo "🔧  Bumping version to $VERSION..."
sed -i '' "s/APP_VERSION = \".*\"/APP_VERSION = \"$VERSION\"/" updater.py
cat > version.json << EOF
{
  "version": "$VERSION",
  "download_url": "https://github.com/pd-wayne/pdx-onsite-app/releases/latest/download/PDX_Onsite.exe",
  "release_notes": "$NOTES"
}
EOF

echo "📦  Committing..."
git add updater.py version.json
git commit -m "v$VERSION — $NOTES"
git push origin main

echo "🚀  Creating GitHub release v$VERSION..."
gh release create "v$VERSION" "$EXE" \
  --title "PDX Onsite v$VERSION" \
  --notes "$NOTES"

echo ""
echo "✅  Done! v$VERSION is live."
echo "    Download: https://github.com/pd-wayne/pdx-onsite-app/releases/latest/download/PDX_Onsite.exe"
