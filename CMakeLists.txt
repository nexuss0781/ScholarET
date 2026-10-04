cmake_minimum_required(VERSION 3.20)
project(scholarextract LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

set(CMAKE_CXX_FLAGS_RELEASE "-O3 -march=native -flto -DNDEBUG")
set(CMAKE_EXE_LINKER_FLAGS_RELEASE "-flto")

find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(MUPDF QUIET mupdf)
endif()

find_path(MUPDF_INCLUDE_DIR
  NAMES mupdf/fitz.h
  HINTS ${MUPDF_INCLUDE_DIRS} ${MUPDF_PKG_INCLUDE_DIRS}
  PATH_SUFFIXES include)

find_library(MUPDF_LIBRARY
  NAMES mupdf
  HINTS ${MUPDF_LIBRARY_DIRS} ${MUPDF_PKG_LIBRARY_DIRS}
  PATH_SUFFIXES lib lib64)

include(FindPackageHandleStandardArgs)
find_package_handle_standard_args(MuPDF REQUIRED_VARS MUPDF_INCLUDE_DIR MUPDF_LIBRARY)

add_executable(extract script/extract.cpp)

target_include_directories(extract PRIVATE ${MUPDF_INCLUDE_DIR})
target_link_libraries(extract PRIVATE ${MUPDF_LIBRARY} pthread)
if(MUPDF_CFLAGS_OTHER)
  target_compile_options(extract PRIVATE ${MUPDF_CFLAGS_OTHER})
endif()
