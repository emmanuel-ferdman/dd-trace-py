FetchContent_Declare(
    googletest
    GIT_REPOSITORY https://github.com/google/googletest.git
    GIT_TAG v1.15.2)
set(gtest_force_shared_crt
    ON
    CACHE BOOL "" FORCE)
set(INSTALL_GTEST
    OFF
    CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(googletest)
include(GoogleTest)
include(AnalysisFunc)

if(DO_VALGRIND)
    find_program(
        VALGRIND_EXECUTABLE
        NAMES valgrind
        PATHS /usr/bin /usr/local/bin)

    if(VALGRIND_EXECUTABLE)
        set(MEMORYCHECK_COMMAND "${VALGRIND_EXECUTABLE}")
        set(MEMORYCHECK_COMMAND_OPTIONS
            "--leak-check=full --show-leak-kinds=definite --errors-for-leak-kinds=definite --trace-children=yes --error-exitcode=1 --log-fd=1 --suppressions=${CMAKE_CURRENT_SOURCE_DIR}/valgrind.supp"
        )
    else()
        message(FATAL_ERROR "Valgrind not found")
    endif()

    include(CTest)
endif()

function(dd_wrapper_add_test name)
    add_executable(${name} ${ARGN})
    target_include_directories(${name} PRIVATE ../include)
    target_link_libraries(${name} PRIVATE gmock gtest_main dd_wrapper)
    add_ddup_config(${name})

    gtest_discover_tests(
        ${name}
        PROPERTIES # We start new threads after fork(), and we want to continue running the tests after that instead of
                   # dying.
                   ENVIRONMENT "TSAN_OPTIONS=die_after_fork=0:suppressions=${CMAKE_CURRENT_SOURCE_DIR}/TSan.supp")

    set_target_properties(${name} PROPERTIES INSTALL_RPATH "$ORIGIN/..")

    if(LIB_INSTALL_DIR)
        install(TARGETS ${name} RUNTIME DESTINATION ${LIB_INSTALL_DIR}/../test)
    endif()
endfunction()

# Add the tests
dd_wrapper_add_test(test_initialization test_initialization.cpp)
dd_wrapper_add_test(test_api test_api.cpp)
dd_wrapper_add_test(test_threading test_threading.cpp)
dd_wrapper_add_test(test_forking test_forking.cpp)
