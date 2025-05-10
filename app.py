from flask import Flask, render_template, request, redirect, url_for, flash
import os
import subprocess
import shutil
import zipfile
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'services'
app.config['SERVICE_FILES_FOLDER'] = '/etc/systemd/system' # Bu dizin sudo yetkisi gerektirebilir
app.secret_key = 'super secret key' # Needed for flash messages

# Ana dizinler yoksa oluşturalım
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

def run_system_command(command_parts):
    """Helper function to run system commands and capture output."""
    try:
        # For commands that modify systemd, sudo is typically required.
        # Prepending 'sudo' to commands that need it.
        if command_parts[0] == 'systemctl' or command_parts[0] == 'rm' and app.config['SERVICE_FILES_FOLDER'] in command_parts[1]:
            if command_parts[0] != 'sudo': # Avoid double sudo
                 command_parts.insert(0, 'sudo')

        result = subprocess.run(command_parts, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            # Log or flash an error message
            error_message = f"Error executing {' '.join(command_parts)}: {result.stderr}"
            flash(error_message, 'error')
            print(error_message) # Also print to console for debugging
            return None # Indicate failure
        return result.stdout.strip()
    except Exception as e:
        error_message = f"Exception executing {' '.join(command_parts)}: {e}"
        flash(error_message, 'error')
        print(error_message)
        return None

@app.route('/')
def index():
    services = get_systemd_services()
    return render_template('index.html', services=services)

def get_systemd_services():
    services = []
    if not os.path.exists(app.config['SERVICE_FILES_FOLDER']):
        flash(f"Service directory {app.config['SERVICE_FILES_FOLDER']} does not exist. Cannot list services.", 'error')
        return services

    try:
        # Ensure the user running this script has read access to SERVICE_FILES_FOLDER
        # For /etc/systemd/system, this might require sudo or specific group membership.
        # If using sudo for Flask app, this will work.
        # If not, this part may fail or return empty if permissions are insufficient.
        possible_service_files = os.listdir(app.config['SERVICE_FILES_FOLDER'])
    except PermissionError:
        flash(f"Permission denied when trying to read {app.config['SERVICE_FILES_FOLDER']}. Run with sudo or check permissions.", "error")
        return services
        
    for service_file in possible_service_files:
        if service_file.endswith(".service"):
            service_file_path = os.path.join(app.config['SERVICE_FILES_FOLDER'], service_file)
            
            status = "unknown"
            description = "N/A"
            working_dir = None

            try:
                with open(service_file_path, 'r') as f:
                    for line in f:
                        line_strip = line.strip()
                        if line_strip.startswith("Description="):
                            description = line_strip.split("=", 1)[1]
                        elif line_strip.startswith("WorkingDirectory="):
                            working_dir = line_strip.split("=", 1)[1]
                        # Optimization: if both found, no need to read further for these two
                        if description != "N/A" and working_dir:
                            break
            except PermissionError:
                print(f"Permission denied reading {service_file_path}. Skipping for description/WD.")
                # flash(f"Error reading details for {service_file}: Permission denied.", "warning") # Too noisy
            except Exception as e:
                print(f"Could not read or parse {service_file_path} for details: {e}")
                # flash(f"Error reading details for {service_file}: {e}", "warning")

            # Only list services whose code appears to be managed by this tool
            # i.e., WorkingDirectory is set, exists, and is under our UPLOAD_FOLDER
            if working_dir and os.path.isdir(working_dir) and \
               os.path.abspath(working_dir).startswith(os.path.abspath(app.config['UPLOAD_FOLDER'])):

                is_active_output = run_system_command(['systemctl', 'is-active', service_file])
                if is_active_output:
                    if is_active_output == "active":
                        status = "active"
                    elif is_active_output == "inactive" or is_active_output == "unknown": # unknown might mean it's not loaded/enabled
                        status = "inactive"
                        # Check if failed
                        is_failed_output = run_system_command(['systemctl', 'is-failed', service_file])
                        if is_failed_output == "failed":
                            status = "failed"
                    else: # E.g. "activating", "deactivating"
                        status = is_active_output

                services.append({
                    "name": service_file,
                    "status": status,
                    "description": description
                })
    return services

@app.route('/create_service', methods=['POST'])
def create_service():
    service_name_input = request.form.get('service_name')
    code_dir_input = request.form.get('code_dir_name') # New field
    description = request.form.get('description')
    exec_start = request.form.get('exec_start')
    github_url = request.form.get('github_url')
    service_files_zip = request.files.get('service_files')

    if not all([service_name_input, description, exec_start]):
        flash("Servis adı, açıklama ve çalıştırılacak komut alanları zorunludur.", "error")
        return redirect(url_for('index'))

    service_name = secure_filename(service_name_input)
    if not service_name:
        flash("Geçersiz servis adı.", "error")
        return redirect(url_for('index'))

    # Determine the directory name for the service code
    if code_dir_input:
        code_folder_name = secure_filename(code_dir_input)
        if not code_folder_name: # If secure_filename made it empty (e.g., input was "../")
            code_folder_name = service_name # Fallback to service_name
            flash("Geçersiz kod dizini adı sağlandı, servis adı kullanılacak.", "warning")
    else:
        code_folder_name = service_name
    
    service_code_path = os.path.join(app.config['UPLOAD_FOLDER'], code_folder_name)
    service_unit_filepath = os.path.join(app.config['SERVICE_FILES_FOLDER'], f"{service_name}.service")

    # Check if service file or code directory already exists
    if os.path.exists(service_unit_filepath):
        flash(f"'{service_name}.service' adlı servis dosyası zaten mevcut. Lütfen farklı bir servis adı seçin.", "error")
        return redirect(url_for('index'))
    
    if os.path.exists(service_code_path):
        flash(f"'{service_code_path}' kod dizini zaten mevcut. Lütfen farklı bir kod dizini adı seçin veya mevcut olanı silin.", "error")
        return redirect(url_for('index'))

    try:
        os.makedirs(service_code_path, exist_ok=True)
    except OSError as e:
        flash(f"Kod dizini oluşturulamadı {service_code_path}: {e}", "error")
        return redirect(url_for('index'))

    # Source code handling
    if github_url:
        git_clone_result = run_system_command(['git', 'clone', github_url, service_code_path])
        if git_clone_result is None:
            shutil.rmtree(service_code_path, ignore_errors=True)
            return redirect(url_for('index'))
        flash(f"GitHub deposu {github_url} adresinden {service_code_path} dizinine klonlandı.", "success")
    elif service_files_zip and service_files_zip.filename != '':
        if not service_files_zip.filename.endswith('.zip'):
            flash("Yüklenen dosya ZIP arşivi olmalıdır.", "error")
            shutil.rmtree(service_code_path, ignore_errors=True)
            return redirect(url_for('index'))
        
        zip_filename = secure_filename(service_files_zip.filename)
        zip_filepath = os.path.join(service_code_path, zip_filename) # Save zip inside its code_path temporarily
        service_files_zip.save(zip_filepath)
        
        try:
            with zipfile.ZipFile(zip_filepath, 'r') as zip_ref:
                zip_ref.extractall(service_code_path)
            os.remove(zip_filepath)
            flash(f"{zip_filename} başarıyla yüklendi ve {service_code_path} dizinine çıkartıldı.", "success")
        except zipfile.BadZipFile:
            flash("Geçersiz veya bozuk ZIP dosyası.", "error")
            shutil.rmtree(service_code_path, ignore_errors=True)
            return redirect(url_for('index'))
        except Exception as e:
            flash(f"ZIP dosyası işlenirken hata oluştu: {e}", "error")
            shutil.rmtree(service_code_path, ignore_errors=True)
            return redirect(url_for('index'))
    else:
        flash("Servis kodu için bir GitHub Repo URL'si vermeli veya bir ZIP dosyası yüklemelisiniz.", "error")
        if os.path.exists(service_code_path): # Clean up the created directory if no code is provided
            shutil.rmtree(service_code_path, ignore_errors=True)
        return redirect(url_for('index'))

    # Create .service file
    # Ensure service_code_path is absolute for WorkingDirectory
    absolute_service_code_path = os.path.abspath(service_code_path)
    service_file_content = f"""[Unit]
Description={description}
After=network.target

[Service]
ExecStart={exec_start}
WorkingDirectory={absolute_service_code_path}
Restart=always
# User=your_user # Consider making this configurable or using a dedicated service user
# Environment="PYTHONUNBUFFERED=1" # Example: if it's a Python app

[Install]
WantedBy=multi-user.target
"""
    # service_unit_filename = f"{service_name}.service" # Already defined
    # service_unit_filepath = os.path.join(app.config['SERVICE_FILES_FOLDER'], service_unit_filename) # Already defined

    try:
        # This write operation will require sudo if SERVICE_FILES_FOLDER is /etc/systemd/system
        # If the app isn't run with sudo, this will fail.
        # A more robust solution for non-sudo app involves a helper script with sudo rights
        # or configuring sudoers for passwordless execution of specific commands.
        with open(service_unit_filepath, 'w') as f:
            f.write(service_file_content)
        flash(f"Service file {service_unit_filepath} created at {service_unit_filepath}", "success")
    except PermissionError:
        flash(f"Permission denied: Could not write service file to {service_unit_filepath}. Run app with sudo or check permissions.", "error")
        shutil.rmtree(service_code_path, ignore_errors=True) # Clean up code if service file fails
        return redirect(url_for('index'))
    except Exception as e:
        flash(f"Error writing service file: {e}", "error")
        shutil.rmtree(service_code_path, ignore_errors=True) # Clean up
        return redirect(url_for('index'))

    # Reload systemd and enable the service
    if run_system_command(['systemctl', 'daemon-reload']) is not None:
        flash("Systemd daemon reloaded.", "info")
        # Optionally enable the service so it starts on boot
        if run_system_command(['systemctl', 'enable', service_unit_filepath]) is not None:
            flash(f"Service {service_unit_filepath} enabled.", "info")
        else:
            flash(f"Failed to enable {service_unit_filepath}. You may need to do it manually.", "warning")
    else:
        flash("Failed to reload systemd daemon. New service might not be recognized.", "error")
        # Consider cleaning up the created service file and code if daemon-reload fails critically
        # For now, we'll leave them and let user handle systemd issues.

    return redirect(url_for('index'))

# Placeholder for other actions (start, stop, restart, delete)
@app.route('/start_service/<service_file_name>')
def start_service(service_file_name):
    # Sanitize service_file_name before using it in a command
    safe_service_name = secure_filename(service_file_name)
    if not safe_service_name.endswith(".service"): # Basic check
        flash("Invalid service file name for start operation.", "error")
        return redirect(url_for('index'))
        
    if run_system_command(['systemctl', 'start', safe_service_name]) is not None:
        flash(f"Service {safe_service_name} started.", "success")
    else:
        flash(f"Failed to start {safe_service_name}.", "error")
    return redirect(url_for('index'))

@app.route('/stop_service/<service_file_name>')
def stop_service(service_file_name):
    safe_service_name = secure_filename(service_file_name)
    if not safe_service_name.endswith(".service"):
        flash("Invalid service file name for stop operation.", "error")
        return redirect(url_for('index'))

    if run_system_command(['systemctl', 'stop', safe_service_name]) is not None:
        flash(f"Service {safe_service_name} stopped.", "success")
    else:
        flash(f"Failed to stop {safe_service_name}.", "error")
    return redirect(url_for('index'))

@app.route('/restart_service/<service_file_name>')
def restart_service(service_file_name):
    safe_service_name = secure_filename(service_file_name)
    if not safe_service_name.endswith(".service"):
        flash("Invalid service file name for restart operation.", "error")
        return redirect(url_for('index'))

    if run_system_command(['systemctl', 'restart', safe_service_name]) is not None:
        flash(f"Service {safe_service_name} restarted.", "success")
    else:
        flash(f"Failed to restart {safe_service_name}.", "error")
    return redirect(url_for('index'))

@app.route('/delete_service/<service_file_name>')
def delete_service(service_file_name):
    safe_service_name = secure_filename(service_file_name)
    if not safe_service_name.endswith(".service"):
        flash("Silme işlemi için geçersiz servis dosyası adı.", "error")
        return redirect(url_for('index'))
    
    service_unit_filepath = os.path.join(app.config['SERVICE_FILES_FOLDER'], safe_service_name)
    service_code_path = None

    # Try to read WorkingDirectory from the service file before potentially deleting it
    if os.path.exists(service_unit_filepath):
        try:
            with open(service_unit_filepath, 'r') as f:
                for line in f:
                    if line.strip().startswith("WorkingDirectory="):
                        service_code_path = line.strip().split("=", 1)[1]
                        break
        except Exception as e:
            flash(f"{safe_service_name} servisinden Çalışma Dizini okunamadı: {e}. Kod dizini manuel olarak silinmelidir.", "warning")
    else:
        flash(f"Servis dosyası {service_unit_filepath} bulunamadı.", "error")
        # No service file, so nothing to stop or disable from systemd perspective either
        # Might still want to clean up code if a code path was assumed by service_name_short
        # However, without WorkingDirectory, we can't be sure what to delete.
        return redirect(url_for('index'))

    # 1. Stop the service (best effort)
    run_system_command(['systemctl', 'stop', safe_service_name]) # Best effort stop

    # 2. Disable the service
    run_system_command(['systemctl', 'disable', safe_service_name])

    # 3. Remove the service file
    if os.path.exists(service_unit_filepath):
        if run_system_command(['rm', service_unit_filepath]) is not None: # rm needs sudo if in /etc/systemd/system
            flash(f"Service file {safe_service_name} deleted.", "info")
        else:
            flash(f"Failed to delete service file {service_unit_filepath}. Check permissions or delete manually.", "error")
            # Proceed with other cleanup steps anyway

    # 4. Reload systemd daemon
    run_system_command(['systemctl', 'daemon-reload'])
    run_system_command(['systemctl', 'reset-failed']) # Clear failed state if any

    # 5. Remove the service code directory
    if os.path.isdir(service_code_path):
        try:
            shutil.rmtree(service_code_path)
            flash(f"Service code directory {service_code_path} deleted.", "info")
        except Exception as e:
            flash(f"Error deleting service code directory {service_code_path}: {e}", "error")
    
    flash(f"Service {service_name_short} and its files have been processed for deletion.", "success")
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5554) 