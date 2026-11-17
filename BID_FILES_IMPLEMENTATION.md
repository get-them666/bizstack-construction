# Bid File Cabinet - Implementation Summary

## Overview
Added a file upload feature to bid cards with folder/category organization for managing permits, photos, inspection reports, contracts, change orders, correspondence, and other bid-related documents.

## Features Implemented

### 1. Database Schema
- **Table**: `bid_files`
- **Columns**:
  - `id` - Primary key
  - `bid_id` - Reference to the bid
  - `filename` - Sanitized filename on disk
  - `original_filename` - Original uploaded filename
  - `file_path` - Relative path from uploads root
  - `file_size` - File size in bytes
  - `mime_type` - MIME type of the file
  - `folder` - Category/folder name (default: "Other")
  - `uploaded_at` - Timestamp
  - `uploaded_by` - Email of uploader

### 2. Default Folders
Eight default categories are available:
1. Plans & Drawings
2. Photos
3. Permits
4. Contracts & Proposals
5. Change Orders
6. Correspondence
7. Inspections
8. Other

Custom folders can be created by typing a new name in the dropdown.

### 3. File Storage
- **Location**: `/opt/bizstack/construction/uploads/bids/{bid_id}/{folder}/{filename}`
- **Structure**: Files are organized by bid ID, then by folder category
- **Naming**: UUID prefix + sanitized original filename to prevent collisions
- **Security**: Files stored outside web root, served through authenticated routes

### 4. API Routes

#### GET `/api/bids/{bid_id}/files`
List all files for a bid, grouped by folder.

#### POST `/api/bids/{bid_id}/files/upload`
Upload one or more files to a bid.
- **Parameters**:
  - `files` - Multiple file upload (multipart/form-data)
  - `folder` - Target folder/category
- **Validation**:
  - File size limit: 50 MB per file
  - Allowed types: PDF, JPG, JPEG, PNG, DOC, DOCX, XLS, XLSX
  - Filename sanitization to prevent directory traversal
- **Response**: Redirects back to bid detail page with success/error counts

#### GET `/api/bids/{bid_id}/files/{file_id}/download`
Download a file. Path validation ensures files cannot escape the uploads directory.

#### POST `/api/bids/{bid_id}/files/{file_id}/delete`
Delete a file from disk and database. Idempotent - missing files on disk are not errors.

### 5. UI Changes

#### Bid Detail Page (`templates/construction/bid_detail.html`)
- **New Section**: "File Cabinet" after the Job Documents section
- **Upload Form**:
  - Folder dropdown with all available categories
  - Multi-file input with drag-and-drop support
  - File type validation on client side
  - Upload button
- **File Display**:
  - Files grouped by folder
  - Each folder shows file count
  - File icons based on MIME type (📄 PDF, 🖼️ images, 📊 spreadsheets, 📝 documents, 📎 other)
  - File size and upload date
  - Download link
  - Delete button with confirmation
- **Status Messages**:
  - Success: Shows count of uploaded files
  - Errors: Shows count and details of failed uploads
- **Quick Access**: Added "Files" link to bottom action bar

### 6. Security Features

1. **Authentication**: All routes require admin access via `require_admin()`
2. **Filename Sanitization**: 
   - Strips path separators and null bytes
   - Prevents directory traversal attacks
3. **Extension Validation**: Only allowed file types can be uploaded
4. **Size Limits**: 50 MB per file prevents abuse
5. **Path Validation**: Download route validates resolved paths stay within uploads root
6. **Files Outside Web Root**: Not served statically, only through authenticated routes

### 7. Testing

Created `test_bid_files.py` with comprehensive tests:
- Filename sanitization
- Default folders definition
- File extension validation
- Database schema creation
- File upload and storage
- File listing by bid
- Folder listing
- File path validation
- File deletion
- Multiple folder support

All tests pass successfully.

### 8. Startup Initialization

Added to `lifecycle()` in `construction_main.py`:
- Creates `/opt/bizstack/construction/uploads/bids` directory on boot
- Logs: `📁 bid files root ready: /opt/bizstack/construction/uploads/bids`
- Configurable via `BID_FILES_ROOT` environment variable

## Files Modified

1. **construction_main.py**
   - Added `bid_files` table DDL (lines 869-886)
   - Added startup directory creation (lines 907-913)
   - Added helper functions: `_bid_files_ensure_schema`, `_sanitize_bid_filename`, `_bid_files_list`, `_bid_files_folders`, `_read_file_in_chunks`
   - Added 4 API routes for file operations
   - Updated `bid_detail_page` to fetch and pass bid files to template

2. **templates/construction/bid_detail.html**
   - Added "File Cabinet" section (95 lines)
   - Added upload form with folder selection
   - Added file display grouped by folder
   - Added "Files" link to bottom action bar

3. **test_bid_files.py** (new file)
   - Comprehensive end-to-end test suite

## Deployment

✅ Committed to git
✅ Deployed to DigitalOcean droplet `bizstack-prod`
✅ Container rebuilt and restarted
✅ Verified bid files root created on startup
✅ Application running successfully

## Usage

1. Navigate to any bid detail page (`/bids/{bid_id}`)
2. Scroll to "File Cabinet" section
3. Select a folder from the dropdown
4. Click "Choose Files" and select one or more files
5. Click "Upload files"
6. Files appear grouped by folder with download and delete options

## Future Enhancements (Optional)

- Drag-and-drop file upload zone
- File preview for images
- Bulk delete/select files
- File search/filter
- Folder rename/reorder
- File comments/notes
- Integration with bid package PDF generation
