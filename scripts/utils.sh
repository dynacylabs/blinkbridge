#!/bin/bash
#
# utils.sh - Logging utilities for BlinkBridge scripts
#
# Provides colored logging functions (debug, info, warn, error) with
# configurable log levels. Uses tput for terminal color support.
#
# Usage:
#   source utils.sh
#   set_log_level "info"
#   debug "This won't appear"
#   info "This will appear"
#

# Terminal color codes using tput for portability
COLOR_RED=$(tput setaf 1)
COLOR_GREEN=$(tput setaf 2)
COLOR_YELLOW=$(tput setaf 3)
COLOR_BLUE=$(tput setaf 4)
COLOR_BOLD=$(tput bold)
COLOR_RESET=$(tput sgr0)

# Default log level (debug shows everything)
LOG_LEVEL="debug"

#######################################
# Set the logging level for output filtering.
# 
# Arguments:
#   $1 - Log level (debug|info|warn|error)
# Globals:
#   LOG_LEVEL - Updated with the new log level
# Returns:
#   None
#######################################
set_log_level() {
  case "$1" in
    debug|info|warn|error)
      export LOG_LEVEL="$1"
      ;;
    *)
      echo "${COLOR_BOLD}${COLOR_RED}Invalid log level: ${1}${COLOR_RESET}"
      ;;
  esac
}

#######################################
# Print debug message in green (lowest priority).
# Only displayed when LOG_LEVEL is 'debug'.
# 
# Arguments:
#   $@ - Message to log
# Returns:
#   None
#######################################
debug() {
  if [[ "$LOG_LEVEL" == "debug" ]]; then
    echo "${COLOR_BOLD}${COLOR_GREEN}debug${COLOR_RESET}: $@"
  fi
}

#######################################
# Print informational message in blue.
# Displayed when LOG_LEVEL is 'debug' or 'info'.
# 
# Arguments:
#   $@ - Message to log
# Returns:
#   None
#######################################
info() {
  if [[ "$LOG_LEVEL" == "debug" || "$LOG_LEVEL" == "info" ]]; then
    echo "${COLOR_BOLD}${COLOR_BLUE}info${COLOR_RESET}: $@"
  fi
}

#######################################
# Print warning message in yellow.
# Displayed unless LOG_LEVEL is 'error'.
# 
# Arguments:
#   $@ - Message to log
# Returns:
#   None
#######################################
warn() {
  if [[ "$LOG_LEVEL" == "debug" || "$LOG_LEVEL" == "info" || "$LOG_LEVEL" == "warn" ]]; then
    echo "${COLOR_BOLD}${COLOR_YELLOW}warn${COLOR_RESET}: $@"
  fi
}

#######################################
# Print error message in red (highest priority).
# Always displayed regardless of LOG_LEVEL.
# 
# Arguments:
#   $@ - Message to log
# Returns:
#   None
#######################################
error() {
  echo "${COLOR_BOLD}${COLOR_RED}error${COLOR_RESET}: $@"
}
