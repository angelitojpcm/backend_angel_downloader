from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import time
import threading
import signal
import psutil
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)

# Configuración
DOWNLOAD_FOLDER = "downloads"
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# Diccionario para almacenar el progreso de las descargas
download_progress = {}
download_processes = {}
download_threads = {}
download_cancel_flags = {}


class ProgressHook:
    def __init__(self, video_id, stage="video"):
        self.video_id = video_id
        self.stage = stage
        self.start_time = time.time()

    def __call__(self, d):
        # Verificar si la descarga fue cancelada
        if (
            self.video_id in download_cancel_flags
            and download_cancel_flags[self.video_id]
        ):
            raise Exception("Descarga cancelada por el usuario")

        if d["status"] == "downloading":
            try:
                total = d.get("total_bytes", 0) or d.get("total_bytes_estimate", 0)
                downloaded = d.get("downloaded_bytes", 0)
                speed = d.get("speed", 0)
                if total and downloaded:
                    progress = (downloaded / total) * 100
                    elapsed_time = time.time() - self.start_time
                    eta = (total - downloaded) / speed if speed > 0 else 0

                    # Actualizar progreso según la etapa
                    if self.stage == "video":
                        base_progress = 0
                    elif self.stage == "audio":
                        base_progress = 45
                    else:  # merging
                        base_progress = 90

                    adjusted_progress = base_progress + (
                        progress * 0.45 if self.stage != "merging" else progress * 0.1
                    )

                    download_progress[self.video_id].update(
                        {
                            "status": f"{self.stage}_downloading",
                            "progress": round(adjusted_progress, 2),
                            "speed": f"{speed/1024/1024:.2f} MB/s",
                            "elapsed": round(elapsed_time, 2),
                            "eta": round(eta, 2),
                            "size_downloaded": f"{downloaded/1024/1024:.2f}MB",
                            "size_total": f"{total/1024/1024:.2f}MB",
                        }
                    )

                    # Verificar cancelación después de cada actualización
                    if (
                        self.video_id in download_cancel_flags
                        and download_cancel_flags[self.video_id]
                    ):
                        raise Exception("Descarga cancelada por el usuario")

            except Exception as e:
                if "cancelled" in str(e):
                    raise e
                print(f"Error en progress hook: {e}")

        elif d["status"] == "finished":
            if self.stage == "video":
                download_progress[self.video_id]["status"] = "downloading_audio"
            elif self.stage == "audio":
                download_progress[self.video_id]["status"] = "merging"
                download_progress[self.video_id]["progress"] = 90
            else:
                download_progress[self.video_id]["status"] = "completed"
                download_progress[self.video_id]["progress"] = 100

        elif d["status"] == "error":
            download_progress[self.video_id]["status"] = "error"


def format_duration(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60

    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_size(bytes):
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes < 1024:
            return f"{bytes:.1f}{unit}"
        bytes /= 1024
    return f"{bytes:.1f}TB"


def optimize_ffmpeg_settings(format_ext):
    """Optimizar configuración de FFmpeg según el formato"""
    if format_ext in ["mp4", "mkv"]:
        return [
            "-c:v",
            "libx264",
            "-preset",
            "fast",  # Usar 'faster' o 'veryfast' para más velocidad
            "-crf",
            "23",  # Balance entre calidad y tamaño
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ]
    elif format_ext == "webm":
        return [
            "-c:v",
            "libvpx-vp9",
            "-cpu-used",
            "4",  # Mayor velocidad
            "-deadline",
            "good",
            "-crf",
            "30",
            "-b:v",
            "0",
            "-c:a",
            "libopus",
            "-b:a",
            "128k",
        ]
    return []


def get_best_audio_format(formats):
    """Obtener el mejor formato de audio disponible"""
    audio_formats = [
        f
        for f in formats
        if f.get("acodec", "none") != "none"
        and f.get("vcodec") == "none"
        and f.get("format_id")
    ]
    if audio_formats:
        return max(
            audio_formats,
            key=lambda x: int(x.get("filesize", 0)) if x.get("filesize") else 0,
        )
    return None


def process_formats(formats):
    """Procesar y filtrar formatos únicos"""
    seen_qualities = set()
    processed_formats = {"videoOnly": [], "audioOnly": [], "combined": []}

    # Ordenar formatos por calidad y tamaño
    for f in formats:
        format_info = {
            "itag": f.get("format_id"),
            "quality": (
                f.get("resolution", "N/A")
                if f.get("resolution")
                else f.get("format_note", "N/A")
            ),
            "container": f.get("ext", ""),
            "size": format_size(f.get("filesize", 0)) if f.get("filesize") else "N/A",
            "raw_size": f.get("filesize", 0),
            "vcodec": f.get("vcodec", "none"),
            "acodec": f.get("acodec", "none"),
            "fps": f.get("fps", 0),
            "format_note": f.get("format_note", ""),
        }

        quality_key = f"{format_info['quality']}_{format_info['container']}"

        # Filtrar formatos duplicados y organizar por tipo
        if format_info["vcodec"] != "none" and format_info["acodec"] != "none":
            if quality_key not in seen_qualities:
                seen_qualities.add(quality_key)
                processed_formats["combined"].append(format_info)
        elif format_info["vcodec"] != "none" and format_info["acodec"] == "none":
            if quality_key not in seen_qualities:
                seen_qualities.add(quality_key)
                processed_formats["videoOnly"].append(format_info)
        elif format_info["vcodec"] == "none" and format_info["acodec"] != "none":
            if quality_key not in seen_qualities:
                seen_qualities.add(quality_key)
                processed_formats["audioOnly"].append(format_info)

    # Ordenar formatos por calidad
    for key in processed_formats:
        processed_formats[key].sort(
            key=lambda x: (
                int(x.get("raw_size", 0)) if x.get("raw_size") else 0,
                x.get("fps", 0),
            ),
            reverse=True,
        )

    return processed_formats


def get_video_info(url):
    """Obtener información del video"""
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = process_formats(info.get("formats", []))

            thumbnails = info.get("thumbnails", [])
            thumbnails.sort(
                key=lambda x: x.get("height", 0) * x.get("width", 0), reverse=True
            )

            return {
                "success": True,
                "data": {
                    "title": info.get("title", ""),
                    "duration": {
                        "seconds": info.get("duration", 0),
                        "formatted": format_duration(info.get("duration", 0)),
                    },
                    "thumbnails": thumbnails,
                    "formats": formats,
                    "author": {
                        "name": info.get("uploader", ""),
                        "url": info.get("uploader_url", ""),
                    },
                    "statistics": {
                        "views": info.get("view_count", 0),
                        "likes": info.get("like_count", 0),
                    },
                    "description": info.get("description", ""),
                },
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_available_formats(url):
    """Obtener lista de formatos disponibles para el video"""
    try:
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("formats", [])
    except Exception as e:
        return []


def validate_format(formats, itag):
    """Validar si un formato específico está disponible"""
    return any(f.get("format_id") == itag for f in formats)


def safe_get_format(formats_dict, itag):
    """Buscar formato de manera segura en el diccionario de formatos"""
    for format_list in formats_dict.values():
        for fmt in format_list:
            if fmt["itag"] == itag:
                return fmt
    return None


def get_best_audio_for_format(formats, video_format):
    """Obtener el mejor formato de audio compatible"""
    audio_formats = [
        f
        for f in formats
        if f.get("acodec", "none") != "none"
        and f.get("vcodec") == "none"
        and f.get("format_id")
    ]

    if not audio_formats:
        return None

    # Ordenar por calidad y tamaño
    return max(
        audio_formats,
        key=lambda x: int(x.get("filesize", 0)) if x.get("filesize") else 0,
    )


def ensure_audio_video_format(format_id, formats):
    """Asegurar que el formato tenga video y audio"""
    selected_format = None

    # Buscar el formato seleccionado
    for f in formats:
        if f.get("format_id") == format_id:
            selected_format = f
            break

    if not selected_format:
        return "best"  # Formato por defecto si no se encuentra

    # Si ya tiene audio y video, retornar el formato original
    if (
        selected_format.get("acodec", "none") != "none"
        and selected_format.get("vcodec", "none") != "none"
    ):
        return format_id

    # Si solo tiene video, buscar el mejor audio
    if (
        selected_format.get("vcodec", "none") != "none"
        and selected_format.get("acodec", "none") == "none"
    ):
        best_audio = get_best_audio_for_format(formats, selected_format)
        if best_audio:
            return f"{format_id}+{best_audio['format_id']}"

    return "best"  # Formato por defecto si no se puede asegurar audio+video


def download_and_merge(url, video_format, audio_format, output_path, video_id):
    """Descargar video y audio por separado y unirlos"""
    try:
        # Asegurar que el output_path sea MP4
        output_path = os.path.splitext(output_path)[0] + ".mp4"

        # Configurar nombres de archivo temporales
        temp_video = os.path.join(DOWNLOAD_FOLDER, f"temp_video_{video_id}.%(ext)s")
        temp_audio = os.path.join(DOWNLOAD_FOLDER, f"temp_audio_{video_id}.%(ext)s")

        video_path = None
        audio_path = None

        # Descargar video
        try:
            video_opts = {
                "format": video_format,
                "outtmpl": temp_video,
                "progress_hooks": [ProgressHook(video_id, "video")],
            }
            with yt_dlp.YoutubeDL(video_opts) as ydl:
                video_info = ydl.extract_info(url, download=True)
                video_path = ydl.prepare_filename(video_info)

                # Verificar si la descarga fue cancelada
                if (
                    video_id in download_progress
                    and download_progress[video_id].get("status") == "cancelled"
                ):
                    raise Exception("Descarga cancelada por el usuario")

        except Exception as e:
            if "cancelled" not in str(e):
                download_progress[video_id]["status"] = "error"
                download_progress[video_id][
                    "error"
                ] = f"Error descargando video: {str(e)}"
            raise e

        # Descargar audio
        try:
            audio_opts = {
                "format": audio_format,
                "outtmpl": temp_audio,
                "progress_hooks": [ProgressHook(video_id, "audio")],
            }
            with yt_dlp.YoutubeDL(audio_opts) as ydl:
                audio_info = ydl.extract_info(url, download=True)
                audio_path = ydl.prepare_filename(audio_info)

                # Verificar si la descarga fue cancelada
                if (
                    video_id in download_progress
                    and download_progress[video_id].get("status") == "cancelled"
                ):
                    raise Exception("Descarga cancelada por el usuario")

        except Exception as e:
            if "cancelled" not in str(e):
                download_progress[video_id]["status"] = "error"
                download_progress[video_id][
                    "error"
                ] = f"Error descargando audio: {str(e)}"
            raise e

        # Actualizar estado a unión
        download_progress[video_id].update({"status": "merging", "progress": 90})

        # Construir comando FFmpeg
        ffmpeg_cmd = [
            "ffmpeg",
            "-i",
            video_path,
            "-i",
            audio_path,
            "-c:v",
            "libx264",  # Codec de video H.264
            "-preset",
            "fast",  # Preset de codificación
            "-crf",
            "23",  # Calidad de video (menor = mejor calidad)
            "-c:a",
            "aac",  # Codec de audio AAC
            "-b:a",
            "192k",  # Bitrate de audio
            "-movflags",
            "+faststart",  # Optimizar para reproducción web
            "-y",  # Sobrescribir archivo si existe
            output_path,
        ]

        # Ejecutar FFmpeg y guardar referencia al proceso
        import subprocess
        import platform

        # Configurar creación de proceso según el sistema operativo
        if platform.system() == "Windows":
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
            )

        download_processes[video_id] = process

        # Esperar a que termine el proceso
        stdout, stderr = process.communicate()

        # Verificar si el proceso fue cancelado o tuvo error
        if video_id in download_progress and (
            download_progress[video_id].get("status") == "cancelled"
            or process.returncode != 0
        ):

            error_msg = (
                "Descarga cancelada por el usuario"
                if download_progress[video_id].get("status") == "cancelled"
                else f"Error en FFmpeg: {stderr.decode()}"
            )

            if "cancelled" not in error_msg:
                download_progress[video_id]["status"] = "error"
                download_progress[video_id]["error"] = error_msg

            raise Exception(error_msg)

        # Limpiar referencia al proceso
        if video_id in download_processes:
            del download_processes[video_id]

        # Actualizar estado a completado
        download_progress[video_id].update({"status": "completed", "progress": 100})

        return True

    except Exception as e:
        if "cancelled" not in str(e) and video_id in download_progress:
            download_progress[video_id]["status"] = "error"
            download_progress[video_id]["error"] = str(e)
        raise e

    finally:
        # Limpiar archivos temporales
        try:
            if video_path and os.path.exists(video_path):
                os.remove(video_path)
        except Exception as e:
            print(f"Error eliminando video temporal: {e}")

        try:
            if audio_path and os.path.exists(audio_path):
                os.remove(audio_path)
        except Exception as e:
            print(f"Error eliminando audio temporal: {e}")

        # Limpiar proceso si existe
        if video_id in download_processes:
            try:
                del download_processes[video_id]
            except:
                pass


@app.route("/yt", methods=["GET"])
def get_info():
    """Endpoint para obtener información del video"""
    url = request.args.get("url")
    if not url:
        return jsonify({"success": False, "error": "URL no proporcionada"}), 400

    info = get_video_info(url)
    return jsonify(info)


@app.route("/yt/progress/<video_id>", methods=["GET"])
def get_progress(video_id):
    """Endpoint para obtener el progreso de la descarga"""
    if video_id in download_progress:
        return jsonify(download_progress[video_id])
    return jsonify({"status": "not_found"})


@app.route("/yt/download", methods=["POST"])
def download():
    """Endpoint para iniciar la descarga de un video"""
    try:
        data = request.get_json()
        if not data or "url" not in data or "itag" not in data:
            return jsonify({"success": False, "error": "Datos incompletos"}), 400

        url = data["url"]
        itag = data["itag"]
        video_id = f"{int(time.time())}"

        # Obtener formatos disponibles
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info["formats"]

        # Encontrar el formato seleccionado
        selected_format = None
        for f in formats:
            if f.get("format_id") == itag:
                selected_format = f
                break

        # Determinar si es una descarga de solo audio
        is_audio_only = selected_format and selected_format.get("vcodec") == "none"

        if is_audio_only:
            # Para audio, asegurarse de que el nombre del archivo final sea correcto
            final_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}_final.mp3")
            temp_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}_temp")

            ydl_opts = {
                "format": itag,
                "outtmpl": temp_path,
                "progress_hooks": [ProgressHook(video_id)],
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                        "nopostoverwrites": False,  # Sobrescribir archivos existentes
                    }
                ],
                "keepvideo": False,  # No mantener el archivo original
                "writethumbnail": False,  # No guardar thumbnail
                "final_filepath": final_path,  # Guardar la ruta final
            }
            download_progress[video_id] = {
                "status": "starting",
                "progress": 0,
                "is_audio": True,
                "title": info.get("title", ""),
                "final_path": final_path,  # Guardar la ruta final en el progreso
            }
        else:
            output_path = os.path.join(DOWNLOAD_FOLDER, f"video_{video_id}.mp4")
            format_string = (
                f"{itag}+bestaudio/best"
                if selected_format.get("acodec") == "none"
                else itag
            )
            ydl_opts = {
                "format": format_string,
                "outtmpl": output_path,
                "progress_hooks": [ProgressHook(video_id)],
                "merge_output_format": "mp4",
                "postprocessor_args": {
                    "ffmpeg": [
                        "-c:v",
                        "libx264",
                        "-preset",
                        "fast",
                        "-crf",
                        "23",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-movflags",
                        "+faststart",
                    ]
                },
            }
            download_progress[video_id] = {
                "status": "starting",
                "progress": 0,
                "is_audio": False,
                "final_path": output_path,
            }

        def download_thread():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

                if is_audio_only:
                    # Renombrar el archivo si es necesario
                    temp_mp3 = f"{temp_path}.mp3"
                    if os.path.exists(temp_mp3):
                        import shutil

                        shutil.move(temp_mp3, final_path)

                download_progress[video_id].update(
                    {"status": "completed", "progress": 100}
                )
            except Exception as e:
                download_progress[video_id].update({"status": "error", "error": str(e)})
                print(f"Error en la descarga: {str(e)}")

        thread = threading.Thread(target=download_thread)
        thread.start()
        download_threads[video_id] = thread

        return jsonify(
            {"success": True, "message": "Descarga iniciada", "video_id": video_id}
        )

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/yt/download/<video_id>", methods=["GET"])
def get_video(video_id):
    """Obtener el archivo descargado"""
    try:
        if video_id not in download_progress:
            return jsonify({"error": "Archivo no encontrado"}), 404

        if download_progress[video_id]["status"] != "completed":
            return jsonify({"error": "Archivo aún no completado"}), 400

        final_path = download_progress[video_id].get("final_path")
        if not final_path or not os.path.exists(final_path):
            return jsonify({"error": "Archivo no encontrado"}), 404

        # Obtener el título del video
        title = download_progress[video_id].get("title", "download")
        # Limpiar el título (mantener algunos caracteres especiales pero eliminar los problemáticos)
        safe_title = "".join(
            c for c in title if c.isalnum() or c in (" ", "-", "_", "|", ".")
        ).rstrip()

        is_audio = download_progress[video_id].get("is_audio", False)
        extension = ".mp3" if is_audio else ".mp4"
        mimetype = "audio/mpeg" if is_audio else "video/mp4"

        # Crear el nombre final con el formato deseado
        download_name = f"Angel Downloader | {safe_title}{extension}"

        response = send_file(
            final_path,
            mimetype=mimetype,
            as_attachment=True,
            download_name=download_name,
        )

        # Asegurar que el archivo se elimine después de enviarlo
        @response.call_on_close
        def on_close():
            try:
                if os.path.exists(final_path):
                    os.remove(final_path)
                # Limpiar cualquier archivo temporal
                base_path = os.path.join(DOWNLOAD_FOLDER, video_id)
                for ext in [".mp3", ".webm", ".m4a", "_temp", "_temp.mp3", ".mp4"]:
                    temp_file = f"{base_path}{ext}"
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
            except Exception as e:
                print(f"Error limpiando archivos: {e}")

        return response

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/yt/cancel/<video_id>", methods=["POST"])
def cancel_download(video_id):
    """Endpoint para cancelar una descarga en progreso"""
    try:
        if video_id not in download_progress:
            return jsonify({"success": False, "error": "Descarga no encontrada"}), 404

        # Marcar la descarga para cancelación
        download_cancel_flags[video_id] = True

        # Actualizar estado inmediatamente
        download_progress[video_id].update(
            {"status": "cancelling", "progress": 0, "error": "Cancelando descarga..."}
        )

        # Terminar procesos activos
        if video_id in download_processes:
            try:
                process = download_processes[video_id]
                import platform

                if platform.system() == "Windows":
                    import signal

                    try:
                        process.send_signal(signal.CTRL_BREAK_EVENT)
                    except:
                        process.kill()
                else:
                    if psutil.pid_exists(process.pid):
                        parent = psutil.Process(process.pid)
                        for child in parent.children(recursive=True):
                            try:
                                child.kill()
                            except:
                                pass
                        parent.kill()
            except Exception as e:
                print(f"Error terminando proceso: {e}")
            finally:
                del download_processes[video_id]

        # Limpiar archivos existentes
        try:
            base_path = os.path.join(DOWNLOAD_FOLDER, video_id)
            patterns = [
                f"video_{video_id}.*",
                f"audio_{video_id}.*",
                f"temp_video_{video_id}.*",
                f"temp_audio_{video_id}.*",
                f"{video_id}_final.*",
                f"{video_id}_temp.*",
            ]
            import glob

            for pattern in patterns:
                for file in glob.glob(os.path.join(DOWNLOAD_FOLDER, pattern)):
                    try:
                        os.remove(file)
                    except Exception as e:
                        print(f"Error eliminando {file}: {e}")
        except Exception as e:
            print(f"Error limpiando archivos: {e}")

        # Actualizar estado final
        download_progress[video_id].update(
            {
                "status": "cancelled",
                "progress": 0,
                "error": "Descarga cancelada por el usuario",
            }
        )

        # Limpiar referencias
        if video_id in download_threads:
            del download_threads[video_id]

        return jsonify(
            {"success": True, "message": "Descarga cancelada", "status": "cancelled"}
        )

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# Función de limpieza (opcional)
def cleanup_downloads():
    """Limpia archivos antiguos del directorio de descargas y estados"""
    try:
        current_time = time.time()

        # Limpiar archivos
        for filename in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.getmtime(filepath) < current_time - 3600:
                try:
                    os.remove(filepath)
                except Exception as e:
                    print(f"Error eliminando {filepath}: {e}")

        # Limpiar estados antiguos
        video_ids = list(download_progress.keys())
        for video_id in video_ids:
            if video_id in download_progress:
                status = download_progress[video_id].get("status")
                if status in ["completed", "error", "cancelled"]:
                    # Limpiar todos los estados relacionados
                    if video_id in download_progress:
                        del download_progress[video_id]
                    if video_id in download_processes:
                        del download_processes[video_id]
                    if video_id in download_threads:
                        del download_threads[video_id]
                    if video_id in download_cancel_flags:
                        del download_cancel_flags[video_id]

    except Exception as e:
        print(f"Error en limpieza: {e}")


# Agregar limpieza periódica
def start_cleanup_scheduler():
    def cleanup_task():
        while True:
            cleanup_downloads()
            time.sleep(300)  # Ejecutar cada 5 minutos

    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()


# Iniciar el planificador de limpieza cuando se inicie la aplicación
if __name__ == "__main__":
    start_cleanup_scheduler()
    app.run(debug=True, port=18013)
