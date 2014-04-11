@echo off
setlocal

set REPOS=%~1
set REV=%~2
set USER=%~3
set PROPNAME=%~4
set ACTION=%~5

rem Print out some debug info
echo pre-revprop-change: REPOS=%REPOS% REV=%REV% USER=%USER% PROPNAME=%PROPNAME% ACTION=%ACTION% >&2

rem Allow modifying certain properties
if "%PROPNAME%" == "svn:author" goto ALLOW
if "%PROPNAME%" == "svn:date" goto ALLOW

rem Reject everyting else
goto DENY

:DENY
echo Not allowed to change %PROPNAME% >&2
endlocal
@echo on
exit 1 

:ALLOW
endlocal
@echo on
exit 0