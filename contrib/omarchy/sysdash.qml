// sysdash health dot: dim when all is well, sand on a warning, clay on a critical alert.
// Click opens the dashboard. Data: GET http://127.0.0.1:8765/api/health (polled every 10 s).
import QtQuick

Item {
  id: root
  property var bar
  property string moduleName
  property var settings

  property string level: "offline"   // ok | warning | critical | offline
  property string summary: "sysdash is not running"

  implicitWidth: 22
  implicitHeight: bar ? bar.barSize : 26

  function poll() {
    var xhr = new XMLHttpRequest()
    xhr.onreadystatechange = function() {
      if (xhr.readyState !== XMLHttpRequest.DONE) return
      if (xhr.status !== 200) { root.level = "offline"; root.summary = "sysdash is not running"; return }
      try {
        var d = JSON.parse(xhr.responseText)
        root.level = d.level
        root.summary = d.alerts.length
          ? d.alerts.map(function(a) { return (a.level === "critical" ? "✕ " : "! ") + a.text }).join("\n")
          : "✓ all systems nominal"
      } catch (e) { root.level = "offline" }
    }
    xhr.open("GET", "http://127.0.0.1:8765/api/health")
    xhr.send()
  }

  Timer { interval: 10000; running: true; repeat: true; triggeredOnStart: true; onTriggered: root.poll() }

  Rectangle {
    anchors.centerIn: parent
    width: 8; height: 8; radius: 4
    color: root.level === "critical" ? "#c38b7b"
         : root.level === "warning" ? "#c9ae86"
         : root.level === "ok" ? (bar ? bar.foreground : "#e2dddc")
         : "transparent"
    opacity: root.level === "ok" ? 0.35 : 1
    border.width: root.level === "offline" ? 1 : 0
    border.color: bar ? bar.foreground : "#8a8588"
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onEntered: if (root.bar) root.bar.showTooltip(root, "sysdash\n" + root.summary)
    onExited: if (root.bar) root.bar.hideTooltip(root)
    onClicked: if (root.bar) root.bar.run("omarchy-launch-or-focus-webapp chrome-localhost__-Default http://localhost:8765")
  }
}
