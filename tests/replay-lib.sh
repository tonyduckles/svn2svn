#!/bin/sh

# Setup a brand-new repo ready for svnreplay
init_replay_repo () {
	REPO=$1
	# Init repo
	rm -rf "$REPO"
	svnadmin create "$REPO"
	# Add pre-revprop-change hook script
	cp ../hook-examples/pre-revprop-change_example.txt "$REPO/hooks/pre-revprop-change"
	chmod 755 "$REPO/hooks/pre-revprop-change"
}
