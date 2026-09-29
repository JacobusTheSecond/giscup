cmake_minimum_required(VERSION 3.24)
project(gis_cup_visibility LANGUAGES CXX)

if(POLICY CMP0167)
    cmake_policy(SET CMP0167 NEW)
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(CMAKE_FIND_PACKAGE_PREFER_CONFIG TRUE)
find_package(Eigen3 3.3 CONFIG REQUIRED NO_MODULE)
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
    pkg_check_modules(CoinUtils QUIET IMPORTED_TARGET GLOBAL coinutils)
    pkg_check_modules(Osi       QUIET IMPORTED_TARGET GLOBAL osi)
    pkg_check_modules(Clp       QUIET IMPORTED_TARGET GLOBAL clp)
    pkg_check_modules(OsiClp    QUIET IMPORTED_TARGET GLOBAL osi-clp)
    pkg_check_modules(Cgl       QUIET IMPORTED_TARGET GLOBAL cgl)
    pkg_check_modules(Cbc       QUIET IMPORTED_TARGET GLOBAL cbc)
    pkg_check_modules(OsiCbc    QUIET IMPORTED_TARGET GLOBAL osi-cbc)
endif()

find_package(GDAL REQUIRED)
find_package(Boost REQUIRED)
find_package(CGAL REQUIRED)
find_package(OpenMP)
find_package(ortools CONFIG REQUIRED)

if(APPLE)
    get_property(_gis_imported_targets DIRECTORY PROPERTY IMPORTED_TARGETS)
    foreach(_gis_target IN LISTS _gis_imported_targets)
        foreach(_gis_property IN ITEMS
                INTERFACE_INCLUDE_DIRECTORIES
                INTERFACE_SYSTEM_INCLUDE_DIRECTORIES)
            get_target_property(_gis_dirs "${_gis_target}" "${_gis_property}")
            if(_gis_dirs AND NOT _gis_dirs MATCHES "-NOTFOUND$")
                set(_gis_filtered_dirs "")
                foreach(_gis_dir IN LISTS _gis_dirs)
                    if(NOT _gis_dir MATCHES "/SDKs/MacOSX[^/]*/usr/include/?$")
                        list(APPEND _gis_filtered_dirs "${_gis_dir}")
                    endif()
                endforeach()
                set_property(TARGET "${_gis_target}" PROPERTY
                    "${_gis_property}" "${_gis_filtered_dirs}")
            endif()
        endforeach()
    endforeach()
endif()

function(configure_gis_solver target source)
    add_executable(${target} ${source})
    target_link_libraries(${target} PRIVATE
        GDAL::GDAL
        Boost::boost
        CGAL::CGAL
        ortools::ortools)
    if(OpenMP_CXX_FOUND)
        target_link_libraries(${target} PRIVATE OpenMP::OpenMP_CXX)
        target_compile_definitions(${target} PRIVATE GIS_CUP_HAS_OPENMP=1)
    endif()
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /permissive-)
    else()
        target_compile_options(${target} PRIVATE
            -Wall -Wextra -Wpedantic -Wconversion -Wshadow)
    endif()
endfunction()

# One production executable owns exact-CGAL preprocessing, incremental
# construction, elite fixed-k search, state persistence, and final verification.
# Legacy source checks look for: add_executable(gis_cup_visibility src/main.cpp)
configure_gis_solver(gis_cup_visibility src/main.cpp)
