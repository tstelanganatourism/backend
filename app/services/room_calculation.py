import math

def calculate_required_rooms(total_passengers: int, room_capacity: int) -> int:
    """
    Calculates the number of rooms required for a given number of passengers and room capacity.
    
    Logic: ceil(total_passengers / room_capacity)
    
    Examples:
    1 passenger, capacity 4 = 1 room
    4 passengers, capacity 4 = 1 room
    5 passengers, capacity 4 = 2 rooms
    9 passengers, capacity 4 = 3 rooms
    """
    if total_passengers <= 0:
        return 0
    if room_capacity <= 0:
        raise ValueError("Room capacity must be greater than 0")
        
    return math.ceil(total_passengers / room_capacity)
