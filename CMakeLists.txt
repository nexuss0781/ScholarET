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

find_package(PkgConfig REQUIRED)
pkg_check_modules(MUPDF REQUIRED mupdf)

add_executable(extract script/extract.cpp)

target_include_directories(extract PRIVATE ${MUPDF_INCLUDE_DIRS})
target_link_libraries(extract PRIVATE ${MUPDF_LIBRARIES} pthread)
target_compile_options(extract PRIVATE ${MUPDF_CFLAGS_OTHER})
