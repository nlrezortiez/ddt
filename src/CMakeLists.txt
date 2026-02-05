# Copyright 2012 Jared Boone
# Copyright 2013 Benjamin Vernoux
#
# This file is part of HackRF.
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; see the file COPYING.  If not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Boston, MA 02110-1301, USA.

cmake_minimum_required(VERSION 3.10.0)
project(hackrf-tools LANGUAGES C)
include(GNUInstallDirs)

include(${PROJECT_SOURCE_DIR}/../cmake/set_release.cmake)
add_definitions(-DTOOL_RELEASE="${RELEASE}")
set(CMAKE_MODULE_PATH ${PROJECT_SOURCE_DIR}/../cmake/modules)


add_definitions(-Wall)

if(NOT libhackrf_SOURCE_DIR)
  if(LIBHACKRF_INCLUDE_DIR
     AND LIBHACKRF_LIBRARIES
     AND NOT TARGET HackRF::hackrf)
    add_library(HackRF::hackrf INTERFACE IMPORTED)
    set_target_properties(
      HackRF::hackrf
      PROPERTIES INTERFACE_INCLUDE_DIRECTORIES "${LIBHACKRF_INCLUDE_DIR}"
                 INTERFACE_LINK_LIBRARIES "${LIBHACKRF_LIBRARIES}"
                 RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE`_DIR}/bin")
  else()
    find_package(HackRF REQUIRED)
  endif()
endif()

find_package(FFTW3f)

if(TARGET HackRF::hackrf)
  list(APPEND SWEEP_LINK_LIBS HackRF::hackrf)
else()
  list(APPEND SWEEP_LINK_LIBS HackRF::hackrf_static)
endif()

include(CheckLibraryExists)
check_library_exists(m log10 "" LIBM)

if(LIBM)
  list(APPEND SWEEP_LINK_LIBS m)
endif()

if(FFTW3f_FOUND)
  add_executable(sweep hackrf_sweep.c)
  target_compile_features(sweep PRIVATE c_std_90)
  target_link_libraries(sweep fftw3f::fftw3f ${SWEEP_LINK_LIBS})
  install(TARGETS sweep RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
else()
  message(
    STATUS
      "Not building sweep tool. (Install FFTW)"
  )
endif()

########################################################################
# Create uninstall target
########################################################################

if(NOT HackRF_SOURCE_DIR)
  configure_file(${PROJECT_SOURCE_DIR}/../cmake/cmake_uninstall.cmake.in
                 ${CMAKE_CURRENT_BINARY_DIR}/cmake_uninstall.cmake @ONLY)

  add_custom_target(
    uninstall
    COMMENT "Provide hackrf-tools uninstall target"
    COMMAND ${CMAKE_COMMAND} -P
            ${CMAKE_CURRENT_BINARY_DIR}/cmake_uninstall.cmake)
endif()
