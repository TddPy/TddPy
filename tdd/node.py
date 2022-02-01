from __future__ import annotations
from ast import Index
from typing import Dict, List, Tuple, Union, cast
from . import CUDAcpl
from .CUDAcpl import CUDAcpl_Tensor,_U_,CUDAcpl2np
import torch

from graphviz import Digraph

IndexOrder = List[int]

TERMINAL_ID = -1

def order_inverse(index_order: IndexOrder) -> IndexOrder:
    '''
        Return the "inverse" of the given index order.
        (it can be understand as the inverse in the permutation group.)
    '''
    res = [0]*len(index_order)

    for i in range(len(index_order)):
        res[index_order[i]] = i
    
    return res

class Node:
    '''
        The node used in tdd.
    '''
    
    
    EPS=0.000001
    '''
        The precision for comparing two float numbers.
        It also decides the precision of weights stored in unique_table.
    '''


    @staticmethod
    def get_int_key_long(weight: CUDAcpl_Tensor):
        return torch.round(weight/Node.EPS).long()

    @staticmethod
    def get_int_key_int(weight: CUDAcpl_Tensor):
        return torch.round(weight/Node.EPS).int()

    @staticmethod
    def get_int_key_short(weight: CUDAcpl_Tensor):
        return torch.round(weight/Node.EPS).short()

    
    get_int_key = get_int_key_long  #this function takes in the weight tensor and generates the integer key for unique_table


    __unique_table = dict() 
    '''
        The unique_table to store all the node instances used in tdd.
    '''
    global_node_id = 0 #it counts the total number of nonterminal nodes

    @staticmethod
    def reset():
        Node.__unique_table.clear()
        Node.global_node_id = 0
        

    def __init__(self,id: int, order: int, out_weights: CUDAcpl_Tensor, successors: List[Node|None]):
        '''
        The structure of node instances:
        - id
        - order : represent the order of this node (which tensor index it represent)
        - out_weights : torch.Tensor, shape: [succ_num, ..., 2]. The first index is for the successors,
                        and the last index is for complex representation.
        - successor : terminal nodes are represented by None in the successors.
        '''
        self.id : int = id
        self.order : int = order
        self.out_weights : CUDAcpl_Tensor = out_weights
        self.successors : List[Node|None] = successors

    @property
    def index_range(self) -> int:
        # how many values this index can take
        return len(self.successors)

    @property
    def unique_key(self) -> Tuple:
        '''
            The unique key of this node.
        '''
        return Node.get_unique_key(self.order,self.out_weights,self.successors)
        
    
    @staticmethod
    def get_unique_key(order:int, out_weights: CUDAcpl_Tensor, succ_nodes: List[Node|None]) -> Tuple:
        '''
            unique dictionary key:
                TERMINAL_KEY for terminal node
                [order, index(weight1), index(weight2), ..., successor1, successor2,...] for non-terminal nodes
        '''
        temp_key = tuple(cast(List[Union[int,Node]],[order]) 
                    + cast(List[Union[int,Node]],Node.get_int_key(out_weights).view(-1).tolist()) 
                    + cast(List[Union[int,Node]],succ_nodes))
        return temp_key

    @staticmethod
    def get_unique_node(order:int, out_weights: CUDAcpl_Tensor, succ_nodes: List[Node|None]) -> Node:
        '''
            Return the required node. It is either from the unique table, or a newly created one.
            
            order: represent the order of this node (which tensor index it represent)
            out_weights: the incoming weights of this node, shape: [succ_num, ..., 2].
            succ_nodes: the successor nodes.

            Note: The equality checking inside is conducted with the node.EPS tolerance. So feel free
                    to pass in the raw weights from calculation.
        '''
                
        #generate the unique key
        temp_key = Node.get_unique_key(order, out_weights, succ_nodes)

        if temp_key in Node.__unique_table:
            return Node.__unique_table[temp_key]
        else:
            Node.global_node_id += 1
            id = Node.global_node_id
            successors = succ_nodes.copy()
            res = Node(id, order, out_weights.clone().detach(),successors)
            Node.__unique_table[temp_key] = res
            return res

    @staticmethod
    def duplicate(node: Node|None, parallel_shape: List[int], init_order: int=0,
                 extra_shape_ahead: Tuple= (), extra_shape_behind: Tuple=()) -> Node|None:
        '''
            Duplicate from this node, with the initial order init_order,
            and broadcast it to contain the extra (parallel index) shape ahead and behind.
        '''

        if node == None:
            return None

        order = node.order + init_order
        #broadcast to contain the extra shape
        weights = node.out_weights.view((node.index_range,)+len(extra_shape_ahead)*(1,)
                                    +tuple(parallel_shape)+len(extra_shape_behind)*(1,)+(2,))
        weights = weights.broadcast_to((node.index_range,)+extra_shape_ahead+tuple(parallel_shape)
                                    +extra_shape_behind + (2,))
        successors = [Node.duplicate(successor,parallel_shape,init_order,extra_shape_ahead)
                         for successor in node.successors]
        return Node.get_unique_node(order,weights,successors)

    @staticmethod
    def __direct_append(a: Node|None, b: Node|None) -> Node|None:

        if a == None:
            return b

        new_successors = []
        for succ in a.successors:
            new_successors.append(Node.__direct_append(succ,b))
        
        return Node.get_unique_node(a.order,a.out_weights,new_successors)

    @staticmethod
    def append(a: Node|None, parallel_shape_a: List[int], depth: int,
                 b: Node|None, parallel_shape_b: List[int], parallel_tensor = False)-> Node|None:
        '''
            Replace the terminal node in this graph with 'node', and return the result.

            depth: the depth from this node on, i.e. the number of dims corresponding to this node.
            parallel_tensor: whether to tensor on the parallel indices

            Node: it should be considered merely as an operation on node structures, with no meaning in the tensor regime.
        '''
        if not parallel_tensor:
            modifided_node = Node.duplicate(b,parallel_shape_b,depth)
            return Node.__direct_append(a,modifided_node)
        else:
            b_node_broadcasted = Node.duplicate(b,parallel_shape_b,depth,tuple(parallel_shape_a),())
            a_node_broadcasted = Node.duplicate(a,parallel_shape_a,0,(),tuple(parallel_shape_b))
            return Node.__direct_append(a_node_broadcasted,b_node_broadcasted)


    @staticmethod
    def layout(node: Node|None, parallel_shape: List[int], index_order: List[int],
                 dot=Digraph(), succ: List=[], real_label: bool=True, full_output: bool=False):
        '''
            full_output: if True, then the edge will appear as a tensor, not the parallel index shape.

            (NO TYPING SYSTEM VERIFICATION)
        '''


        col=['red','blue','black','green']

        if node == None:
            id_str = str(TERMINAL_ID)
            label = str(1)
        else:
            id_str = str(node.id)
            label = 'i'+str(index_order[node.order])


        if real_label:
            dot.node(id_str, label, fontname="helvetica",shape="circle",color="red")
        else:
            dot.node(id_str, label, fontname="helvetica",shape="circle",color="red")

        if node:
            for k in range(node.index_range):
                #if there is no parallel index, directly demonstrate the edge values
                if list(node.out_weights[0].shape) == [2]:
                    label1=str(complex(round(node.out_weights[k][0].cpu().item(),2),round(node.out_weights[k][1].cpu().item().imag,2)))
                #otherwise, demonstrate the parallel index shape
                else:
                    if full_output:
                        label1 = str(CUDAcpl2np(node.out_weights[k]))
                    else:
                        label1 = str(list(parallel_shape))
                
                temp = node.successors[k]
                id_str = str(TERMINAL_ID) if temp == None else str(temp.id)
                
                if not node.successors[k] in succ:
                    dot=Node.layout(node.successors[k],parallel_shape,index_order, dot,succ,real_label,full_output)
                    dot.edge(str(node.id),id_str,color=col[k%4],label=label1)
                    succ.append(node.successors[k])
                else:
                    dot.edge(str(node.id),id_str,color=col[k%4],label=label1)
        return dot        
