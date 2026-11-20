using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEngine;

/// <summary>
/// Blendermp cross-engine receiver for Unity.
///
/// Drop this file into Assets/, add the component to an empty GameObject, and
/// press Play while the Blendermp bridge is running (Blender side: N-panel >
/// Cross-engine Bridge > Start). The component connects over TCP, reads
/// newline-delimited JSON, and mirrors object transforms, visibility, colours
/// and (for meshes under the vertex cap) triangle geometry.
///
/// The bridge sends Blender world-space transforms already converted to Unity's
/// Y-up space. Mesh vertices arrive in Blender local space, so this script
/// converts them with (x, y, z) -> (x, z, -y) and reverses triangle winding.
/// </summary>
public class BlendermpReceiver : MonoBehaviour
{
    [Header("Bridge")]
    public string host = "127.0.0.1";
    public int port = 7103;
    public float worldScale = 1f;

    [Header("Debug")]
    public bool logMessages = false;

    private TcpClient _client;
    private Thread _thread;
    private volatile bool _running;
    private readonly ConcurrentQueue<string> _lines = new ConcurrentQueue<string>();
    private readonly Dictionary<string, GameObject> _objects = new Dictionary<string, GameObject>();
    private readonly Dictionary<string, Color> _materials = new Dictionary<string, Color>();

    [Serializable]
    public class Metadata
    {
        public string type;
        public string parent;
        public string[] materials;
    }

    [Serializable]
    public class MeshData
    {
        public float[] positions;
        public float[] normals;
        public int[] indices;
    }

    [Serializable]
    public class MaterialData
    {
        public string name;
        public float[] color;
        public float metallic;
        public float roughness;
    }

    [Serializable]
    public class Message
    {
        public string type;
        public string uid;
        public string name;
        public string kind;
        public float[] matrix;
        public float[] position;
        public float[] rotation;
        public float[] scale;
        public bool visible;
        public Metadata metadata;
        public MeshData mesh;
        public Message[] objects;
        public MaterialData[] materials;
    }

    private void Start()
    {
        Connect();
    }

    private void OnDestroy()
    {
        Disconnect();
    }

    public void Connect()
    {
        if (_running) return;
        _running = true;
        _thread = new Thread(ReadLoop) { IsBackground = true, Name = "BlendermpReceiver" };
        _thread.Start();
    }

    public void Disconnect()
    {
        _running = false;
        try { _client?.Close(); } catch { }
        _client = null;
    }

    private void ReadLoop()
    {
        while (_running)
        {
            try
            {
                _client = new TcpClient();
                _client.Connect(host, port);
                using (var stream = _client.GetStream())
                using (var reader = new StreamReader(stream, Encoding.UTF8))
                {
                    string line;
                    while (_running && (line = reader.ReadLine()) != null)
                    {
                        if (!string.IsNullOrWhiteSpace(line))
                            _lines.Enqueue(line);
                    }
                }
            }
            catch (Exception error)
            {
                if (_running)
                    Debug.LogWarning($"[Blendermp] bridge connection failed: {error.Message}");
            }
            Thread.Sleep(1000);
        }
    }

    private void Update()
    {
        while (_lines.TryDequeue(out var line))
        {
            Message message;
            try { message = JsonUtility.FromJson<Message>(line); }
            catch (Exception error)
            {
                Debug.LogWarning($"[Blendermp] bad message: {error.Message}");
                continue;
            }
            if (message == null) continue;
            if (logMessages) Debug.Log($"[Blendermp] {message.type}");
            switch (message.type)
            {
                case "scene":
                    if (message.materials != null)
                        foreach (var material in message.materials) StoreMaterial(material);
                    if (message.objects != null)
                        foreach (var item in message.objects) ApplyObject(item);
                    break;
                case "materials":
                    if (message.materials != null)
                        foreach (var material in message.materials) StoreMaterial(material);
                    break;
                case "object":
                    ApplyObject(message);
                    break;
                case "remove":
                    RemoveObject(message.uid);
                    break;
            }
        }
    }

    private void StoreMaterial(MaterialData data)
    {
        if (data == null || string.IsNullOrEmpty(data.name) || data.color == null || data.color.Length < 3)
            return;
        var color = new Color(data.color[0], data.color[1], data.color[2],
            data.color.Length > 3 ? data.color[3] : 1f);
        _materials[data.name] = color;
    }

    private void ApplyObject(Message message)
    {
        if (message == null || string.IsNullOrEmpty(message.uid)) return;
        if (!_objects.TryGetValue(message.uid, out var target) || target == null)
        {
            target = new GameObject(string.IsNullOrEmpty(message.name) ? message.uid : message.name);
            _objects[message.uid] = target;
            if (message.kind == "mesh")
            {
                target.AddComponent<MeshFilter>();
                target.AddComponent<MeshRenderer>();
            }
        }
        target.name = string.IsNullOrEmpty(message.name) ? message.uid : message.name;
        target.SetActive(message.visible);
        ApplyTransform(target, message);
        if (message.mesh != null && message.mesh.positions != null && message.mesh.positions.Length >= 9)
            ApplyMesh(target, message);
        ApplyColor(target, message);
        ApplyParent(target, message);
    }

    private void ApplyTransform(GameObject target, Message message)
    {
        if (message.position != null && message.position.Length >= 3)
        {
            target.transform.position = new Vector3(
                message.position[0], message.position[1], message.position[2]) * worldScale;
        }
        if (message.rotation != null && message.rotation.Length >= 4)
        {
            target.transform.rotation = new Quaternion(
                message.rotation[0], message.rotation[1], message.rotation[2], message.rotation[3]);
        }
        if (message.scale != null && message.scale.Length >= 3)
        {
            target.transform.localScale = new Vector3(
                message.scale[0], message.scale[1], message.scale[2]) * worldScale;
        }
    }

    private void ApplyMesh(GameObject target, Message message)
    {
        var filter = target.GetComponent<MeshFilter>();
        if (filter == null) return;
        var positions = message.mesh.positions;
        var indices = message.mesh.indices ?? Array.Empty<int>();
        if (indices.Length < 3) return;

        var vertices = new Vector3[positions.Length / 3];
        for (int index = 0; index < vertices.Length; index++)
        {
            float x = positions[index * 3];
            float y = positions[index * 3 + 1];
            float z = positions[index * 3 + 2];
            vertices[index] = new Vector3(x, z, -y);
        }

        var triangles = new int[indices.Length];
        for (int index = 0; index < indices.Length; index += 3)
        {
            triangles[index] = indices[index];
            triangles[index + 1] = indices[index + 2];
            triangles[index + 2] = indices[index + 1];
        }

        var mesh = new Mesh { name = target.name };
        if (vertices.Length > 65000) mesh.indexFormat = UnityEngine.Rendering.IndexFormat.UInt32;
        mesh.vertices = vertices;
        mesh.triangles = triangles;
        mesh.RecalculateNormals();
        mesh.RecalculateBounds();
        filter.sharedMesh = mesh;
    }

    private void ApplyColor(GameObject target, Message message)
    {
        var renderer = target.GetComponent<MeshRenderer>();
        if (renderer == null) return;
        Color color = new Color(0.8f, 0.8f, 0.8f, 1f);
        if (message.metadata != null && message.metadata.materials != null)
        {
            foreach (var name in message.metadata.materials)
            {
                if (_materials.TryGetValue(name, out var found))
                {
                    color = found;
                    break;
                }
            }
        }
        var material = renderer.sharedMaterial;
        if (material == null)
        {
            var shader = Shader.Find("Standard");
            if (shader == null) return;
            material = new Material(shader);
            renderer.sharedMaterial = material;
        }
        material.color = color;
    }

    private void ApplyParent(GameObject target, Message message)
    {
        if (message.metadata == null || string.IsNullOrEmpty(message.metadata.parent)) return;
        if (_objects.TryGetValue(message.metadata.parent, out var parent) && parent != null)
            target.transform.SetParent(parent.transform, true);
    }

    private void RemoveObject(string uid)
    {
        if (string.IsNullOrEmpty(uid)) return;
        if (_objects.TryGetValue(uid, out var target))
        {
            if (target != null) Destroy(target);
            _objects.Remove(uid);
        }
    }
}
