#!/bin/sh
# Create a reference repo with both /trunk and /branches history

show_last_commit() {
    LOG=$(svn log -l1 $REPOURL)
    revision=$(echo "$LOG" | head -n 2 | tail -n 1| cut -d \| -f 1)
    comment=$(echo "$LOG" | head -n 4 | tail -n 1)
    _WC="${WC//\//\\/}"
    if [ -x $WC ]; then
        len=$(expr ${#REPOURL} + 7)
        url=$(svn info $WC | grep "URL:" | cut -c$len-)
        url="($url)"
    fi
    printf "%-6s%-18s%s\n" "$revision" "$url" "$comment"
}

svn_commit() {
    svn ci -q -m "$1" $2
    svn up -q
    show_last_commit
}


PWD=$(pwd)
REPO="$PWD/_repo_ref"
REPOURL="file://$REPO"
WC="$PWD/_wc_ref"

# Clean-up
echo "Cleaning-up..."
rm -rf $REPO $WC

# Init repo
echo "Creating _repo_ref..."
svnadmin create $REPO
svn mkdir -q -m "Add /trunk" $REPOURL/trunk
show_last_commit
svn mkdir -q -m "Add /branches" $REPOURL/branches
show_last_commit
TRUNK="$REPOURL/trunk"
svn co -q $TRUNK $WC
cd $WC

# Initial Population
mkdir -p $WC/Module/ProjectA
echo "Module/ProjectA/FileA1.txt" > $WC/Module/ProjectA/FileA1.txt
echo "Module/ProjectA/FileA2.txt" > $WC/Module/ProjectA/FileA2.txt
echo "Module/ProjectA/FileA3.txt" > $WC/Module/ProjectA/FileA3.txt
svn -q add $WC/Module
svn_commit "Initial population"

# Add new file
# * Test simple copy-from branch
BRANCH="$REPOURL/branches/fix1"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
mkdir -p $WC/Module/ProjectB
echo "Module/ProjectB/FileB1.txt" > $WC/Module/ProjectB/FileB1.txt
echo "Module/ProjectB/FileB2.txt" > $WC/Module/ProjectB/FileB2.txt
svn add -q $WC/Module/ProjectB
svn_commit "Fix 1: Add Module/ProjectB"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Fix 1: Add Module/ProjectB"

# Rename files
# * Test rename support
# * Test committing rename in two different branch commits: first deletion, then add
BRANCH="$REPOURL/branches/fix2"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
svn mv -q Module/ProjectA/FileA2.txt Module/ProjectB/FileB3.txt
svn_commit "Fix 2: Rename Module/ProjectA/FileA2.txt -> Module/ProjectB/FileB3.txt (part 1 of 2)" Module/ProjectA
svn_commit "Fix 2: Rename Module/ProjectA/FileA2.txt -> Module/ProjectB/FileB3.txt (part 2 of 2)" Module/ProjectB
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Fix 2: Rename Module/ProjectA/FileA2.txt -> Module/ProjectB/FileB3.txt"

# Verify rename
BRANCH="$REPOURL/branches/fix3"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
echo "Module/ProjectB/FileB3.txt (from Fix 3)" >> $WC/Module/ProjectB/FileB3.txt
svn_commit "Fix 3: Modify Module/ProjectB/FileB3.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Fix 3: Modify Module/ProjectB/FileB3.txt"

# Rename files + folders
# * Test rename support
# * Create complicated find-ancestors case, where files/folders are renamed multiple times on branch
BRANCH="$REPOURL/branches/fix4"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
svn mv -q Module/ProjectB Module/ProjectC
svn mv -q Module/ProjectC/FileB1.txt Module/ProjectC/FileC1.txt
echo "Module/ProjectC/FileC1.txt" >> $WC/Module/ProjectC/FileC1.txt
svn mv -q Module/ProjectC/FileB2.txt Module/ProjectC/FileC2.txt
echo "Module/ProjectC/FileC2.txt" >> $WC/Module/ProjectC/FileC2.txt
svn mv -q Module/ProjectC/FileB3.txt Module/ProjectC/FileC3.txt
echo "Module/ProjectC/FileC3.txt" >> $WC/Module/ProjectC/FileC3.txt
svn_commit "Fix 4: Rename Module/ProjectB -> Module/ProjectC"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Fix 4: Rename Module/ProjectB -> Module/ProjectC"

# Verify rename
BRANCH="$REPOURL/branches/fix5"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
echo "Module/ProjectC/FileC1.txt (from Fix 5)" >> $WC/Module/ProjectC/FileC1.txt
echo "Module/ProjectC/FileC2.txt (from Fix 5)" >> $WC/Module/ProjectC/FileC2.txt
echo "Module/ProjectC/FileC3.txt (from Fix 5)" >> $WC/Module/ProjectC/FileC3.txt
svn_commit "Fix 5: Modify Module/ProjectC/FileC*.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Fix 5: Modify Module/ProjectC/FileC*.txt"

# Copy from older revision
svn copy -q -r 8 $TRUNK/Module/ProjectA/FileA2.txt@8 $WC/Module/ProjectA/FileA2.txt
svn_commit "Fix 6: Restore Module/ProjectA/FileA2.txt"

# Verify copy
BRANCH="$REPOURL/branches/fix7"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
echo "Module/ProjectA/FileA2.txt (from Fix 7)" >> $WC/Module/ProjectA/FileA2.txt
svn_commit "Fix 7: Modify Module/ProjectA/FileA2.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Fix 7: Modify Module/ProjectA/FileA2.txt"

