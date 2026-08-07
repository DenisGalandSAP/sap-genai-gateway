import csv

def count_rows_in_csv(file_path):
    with open(file_path, mode='r', newline='') as file:
        reader = csv.reader(file)
        row_count = sum(1 for row in reader)
    print(f"Total number of rows: {row_count}")

# Example usage
count_rows_in_csv('example.csv')
