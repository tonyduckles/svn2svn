#!/bin/sh

# if --tee was passed, write the output not only to the terminal, but
# additionally to the file test-results/$BASENAME.out, too.
case "$SVN2SVN_TEST_TEE_STARTED, $* " in
done,*)
	# do not redirect again
	;;
*' --tee '*|*' --va'*)
	mkdir -p test-results
	BASE=test-results/$(basename "$0" .sh)
	(SVN2SVN_TEST_TEE_STARTED=done ${SHELL-sh} "$0" "$@" 2>&1;
	 echo $? > $BASE.exit) | tee $BASE.out
	test "$(cat $BASE.exit)" = 0
	exit
	;;
esac

# Check if terminal supports color
[ "x$TERM" != "xdumb" ] && (
		[ -t 1 ] &&
		tput bold >/dev/null 2>&1 &&
		tput setaf 1 >/dev/null 2>&1 &&
		tput sgr0 >/dev/null 2>&1
	) &&
	color=t

# Handle test options
while test "$#" -ne 0
do
	case "$1" in
	-d|--d|--de|--deb|--debu|--debug)
		debug=t; shift ;;
	-i|--i|--im|--imm|--imme|--immed|--immedi|--immedia|--immediat|--immediate)
		immediate=t; shift ;;
	-h|--h|--he|--hel|--help)
		help=t; shift ;;
	-v|--v|--ve|--ver|--verb|--verbo|--verbos|--verbose)
		verbose=t; shift ;;
	-q|--q|--qu|--qui|--quie|--quiet)
		quiet=t; shift ;;
	--no-color)
		color=; shift ;;
	--tee)
		shift ;; # was handled already
	*)
		echo "error: unknown test option '$1'" 	 exit 1 ;;
	esac
done

# Define print-helper tags
if test -n "$color"; then
	say_color () {
		(
		case "$1" in
			error) tput bold; tput setaf 1;;  # bold red
			skip)  tput bold; tput setaf 2;;  # bold green
			pass)  tput setaf 2;;             # green
			info)  tput setaf 3;;             # brown
			*) test -n "$quiet" && return;;
		esac
		shift
		printf "%s" "$*"
		tput sgr0
		echo
		)
	}
else
	say_color() {
		test -z "$1" && test -n "$quiet" && return
		shift
		echo "$*"
	}
fi

error () {
	say_color error "error: $*"
	SVN2SVN_EXIT_OK=t
	exit 1
}

say () {
	say_color info "$*"
}

# Make sure parent test was setup correctly
test "${test_description}" != "" ||
error "Test script did not set test_description."

# Handle --help
if test "$help" = "t"
then
	echo "$test_description"
	exit 0
fi

exec 5>&1
exec 6<&0
if test "$verbose" = "t"
then
	exec 4>&2 3>&1
else
	exec 4>/dev/null 3>/dev/null
fi

test_failure=0
test_count=0
test_fixed=0
test_broken=0
test_success=0

test_external_has_tap=0

die () {
	code=$?
	if test -n "$SVN2SVN_EXIT_OK"
	then
		exit $code
	else
		echo >&5 "FATAL: Unexpected exit with code $code"
		exit 1
	fi
}

SVN2SVN_EXIT_OK=
trap 'die' EXIT

. "${TEST_DIRECTORY:-.}"/test-lib-functions.sh

# You are not expected to call test_ok_ and test_failure_ directly, use
# the text_expect_* functions instead.

test_ok_ () {
	test_success=$(($test_success + 1))
	say_color "" "ok $test_count - $@"
}

test_failure_ () {
	test_failure=$(($test_failure + 1))
	say_color error "not ok - $test_count $1"
	shift
	echo "$@" | sed -e 's/^/#	/'
	test "$immediate" = "" || { SVN2SVN_EXIT_OK=t; exit 1; }
}

test_known_broken_ok_ () {
	test_fixed=$(($test_fixed+1))
	say_color "" "ok $test_count - $@ # TODO known breakage"
}

test_known_broken_failure_ () {
	test_broken=$(($test_broken+1))
	say_color skip "not ok $test_count - $@ # TODO known breakage"
}

test_debug () {
	test "$debug" = "" || eval "$1"
}

test_eval_ () {
	# This is a separate function because some tests use
	# "return" to end a test_expect_success block early.
	eval </dev/null >&3 2>&4 "$*"
}

test_run_ () {
	test_cleanup=:
	expecting_failure=$2
	test_eval_ "$1"
	eval_ret=$?

	if test -z "$immediate" || test $eval_ret = 0 || test -n "$expecting_failure"
	then
		test_eval_ "$test_cleanup"
	fi
	if test "$verbose" = "t" && test -n "$HARNESS_ACTIVE"; then
		echo ""
	fi
	return "$eval_ret"
}

test_skip () {
	test_count=$(($test_count+1))
	to_skip=
	for skp in $SVN2SVN_SKIP_TESTS
	do
		case $this_test.$test_count in
		$skp)
			to_skip=t
			break
		esac
	done
	if test -z "$to_skip" && test -n "$test_prereq" &&
	   ! test_have_prereq "$test_prereq"
	then
		to_skip=t
	fi
	case "$to_skip" in
	t)
		of_prereq=
		if test "$missing_prereq" != "$test_prereq"
		then
			of_prereq=" of $test_prereq"
		fi

		say_color skip >&3 "skipping test: $@"
		say_color skip "ok $test_count # skip $1 (missing $missing_prereq${of_prereq})"
		: true
		;;
	*)
		false
		;;
	esac
}

# stub
test_at_end_hook_ () {
	:
}

test_done () {
	SVN2SVN_EXIT_OK=t

	if test -z "$HARNESS_ACTIVE"; then
		test_results_dir="$TEST_OUTPUT_DIRECTORY/test-results"
		mkdir -p "$test_results_dir"
		test_results_path="$test_results_dir/${0%.sh}-$$.counts"

		cat >>"$test_results_path" <<-EOF
		total $test_count
		success $test_success
		fixed $test_fixed
		broken $test_broken
		failed $test_failure

		EOF
	fi

	if test "$test_fixed" != 0
	then
		say_color pass "# fixed $test_fixed known breakage(s)"
	fi
	if test "$test_broken" != 0
	then
		say_color error "# still have $test_broken known breakage(s)"
		msg="remaining $(($test_count-$test_broken)) test(s)"
	else
		msg="$test_count test(s)"
	fi
	case "$test_failure" in
	0)
		# Maybe print SKIP message
		[ -z "$skip_all" ] || skip_all=" # SKIP $skip_all"

		if test $test_external_has_tap -eq 0; then
			say_color pass "# passed all $msg"
			say "1..$test_count$skip_all"
		fi

		test -d "$remove_trash" &&
		cd "$(dirname "$remove_trash")" &&
		rm -rf "$(basename "$remove_trash")"

		test_at_end_hook_

		exit 0 ;;

	*)
		if test $test_external_has_tap -eq 0; then
			say_color error "# failed $test_failure among $msg"
			say "1..$test_count"
		fi

		exit 1 ;;

	esac
}

# Test the binaries we have just built.  The tests are kept in
# t/ subdirectory and are run in 'trash directory' subdirectory.
if test -z "$TEST_DIRECTORY"
then
	# We allow tests to override this, in case they want to run tests
	# outside of t/, e.g. for running tests on the test library
	# itself.
	TEST_DIRECTORY=$(pwd)
fi
if test -z "$TEST_OUTPUT_DIRECTORY"
then
	# Similarly, override this to store the test-results subdir
	# elsewhere
	TEST_OUTPUT_DIRECTORY=$TEST_DIRECTORY
fi

