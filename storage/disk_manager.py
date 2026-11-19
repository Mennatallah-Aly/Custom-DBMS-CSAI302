

import os


class DiskManager:

    def __init__(self, file_path, page_size=4096):
        self.file_path = file_path
        self.page_size = page_size

        if not os.path.exists(self.file_path):
            open(self.file_path, "wb").close()

        self.file = open(self.file_path, "r+b")

    def create_file(self):
        if not os.path.exists(self.file_path):
            open(self.file_path, "wb").close()

    def get_page_count(self):
        self.file.seek(0, os.SEEK_END)
        file_size = self.file.tell()

        return file_size // self.page_size

    def allocate_page(self):
        page_id = self.get_page_count()

        self.file.seek(0, os.SEEK_END)
        self.file.write(b"\x00" * self.page_size)
        self.file.flush()

        return page_id

    def read_page(self, page_id):
        if page_id < 0 or page_id >= self.get_page_count():
            raise ValueError("Invalid page ID")

        self.file.seek(page_id * self.page_size)
        data = self.file.read(self.page_size)

        return data

    def write_page(self, page_id, data):
        if page_id < 0 or page_id >= self.get_page_count():
            raise ValueError("Invalid page ID")

        if len(data) > self.page_size:
            raise ValueError("Data is larger than the page size")

        data = data.ljust(self.page_size, b"\x00")

        self.file.seek(page_id * self.page_size)
        self.file.write(data)
        self.file.flush()

    def close(self):
        if not self.file.closed:
            self.file.close()
