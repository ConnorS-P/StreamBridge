cmake_minimum_required(VERSION 3.20)
project(StreamBridgePlayer VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_compile_definitions(UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX)

add_executable(StreamBridge.Player
    src/main.cpp
    src/audio_wasapi.cpp
    src/media_foundation_probe.cpp
    src/ipc_server.cpp
    src/backends/player_backends.cpp
)

target_include_directories(StreamBridge.Player PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
)

target_link_libraries(StreamBridge.Player PRIVATE
    ole32
    oleaut32
    uuid
    avrt
    dxgi
    mfplat
    mfuuid
    shlwapi
    advapi32
    user32
    shell32
)

if(MSVC)
    target_compile_options(StreamBridge.Player PRIVATE /W4 /WX /EHsc /utf-8)
endif()
