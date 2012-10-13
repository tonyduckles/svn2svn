#!/bin/sh

test_expect_failure () {
	test "$#" = 3 && { test_prereq=$1; shift; } || test_prereq=
	test "$#" = 2 ||
	error "bug in the test script: not 2 or 3 parameters to test-expect-failure"
	export test_prereq
	if ! test_skip "$@"
	then
		say >&3 "checking known breakage: $2"
		if test_run_ "$2" expecting_failure
		then
			test_failure_ "$1"
		else
			test_ok_ "$1"
		fi
	fi
	echo >&3 ""
}

test_expect_success () {
	test "$#" = 3 && { test_prereq=$1; shift; } || test_prereq=
	test "$#" = 2 ||
	error "bug in the test script: not 2 or 3 parameters to test-expect-success"
	export test_prereq
	if ! test_skip "$@"
	then
		say >&3 "expecting success: $2"
		if test_run_ "$2"
		then
			test_ok_ "$1"
		else
			test_failure_ "$@"
		fi
	fi
	echo >&3 ""
}

