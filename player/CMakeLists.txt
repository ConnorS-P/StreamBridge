cmake_minimum_required(VERSION 3.20)
project(StreamBridgePlayer VERSION 0.1.5 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)
FetchContent_Declare(
    webview2_sdk
    URL "https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/1.0.2903.40"
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
)
FetchContent_MakeAvailable(webview2_sdk)

add_compile_definitions(UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX)

add_executable(StreamBridge.Player
    src/main.cpp
    src/audio_wasapi.cpp
    src/media_foundation_probe.cpp
    src/ipc_server.cpp
    src/backends/player_backends.cpp
)

add_library(StreamBridge.Overlay SHARED
    src/overlay_dll.cpp
)

target_link_libraries(StreamBridge.Overlay PRIVATE
    user32
    gdi32
)

if(MSVC)
    target_compile_options(StreamBridge.Overlay PRIVATE /W4 /WX /EHsc /utf-8)
endif()

target_include_directories(StreamBridge.Player PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${webview2_sdk_SOURCE_DIR}/build/native/include
)

target_link_libraries(StreamBridge.Player PRIVATE
    ${webview2_sdk_SOURCE_DIR}/build/native/x64/WebView2LoaderStatic.lib
    ole32
    oleaut32
    uuid
    avrt
    d3d11
    dxgi
    windowsapp
    mfplat
    mfuuid
    shlwapi
    advapi32
    user32
    gdi32
    shell32
    version
)

if(MSVC)
    target_compile_options(StreamBridge.Player PRIVATE /W4 /WX /EHsc /utf-8)
endif()
